# SPDX-License-Identifier: MIT-0
"""The server-written coverage block and the typed-absence rule (ADR-119, ADR-118 decision 5).

Fixtures here are fictional: a missing day on Campaigns ad spend and a partly reported day on
customer returns. No current bake produces either, because a baked window resolves inside the
sources' common coverage and an answer with a hole is refused, so these states are unit-proven
(delivery slices rule 4).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from campaign_portfolio_support import (
    LABELS,
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
    with_missing_day,
)

from domain.presentation.campaign_attribution import (
    CampaignEvidenceStatusCondition,
    CampaignEvidenceStatusRequest,
    CampaignSelectionCapability,
    campaign_delivery_accepted_statuses,
    campaign_detail_evidence_status,
    campaign_evidence_status,
    uncovered_days,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioMetric,
    CampaignPortfolioRow,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_evidence_status,
)
from domain.presentation.evidence_status import (
    CoverageFact,
    CoverageKind,
    CoverageState,
    EvidenceStatus,
    evidence_status_from_payload,
    evidence_status_payload,
    write_evidence_status,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-119", "ADR-118")]

WEEK = DateRange(date(2026, 9, 9), date(2026, 9, 15))
RETAIL_WEEK = DateRange(date(2026, 9, 5), date(2026, 9, 11))


def _fact(**overrides: object) -> CoverageFact:
    base = CoverageFact("viewable_rate", "Viewable rate", "Delivery detail", CoverageKind.RATIO)
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_a_complete_figure_names_no_dates_and_states_no_absence() -> None:
    status = write_evidence_status(WEEK, (_fact(),))

    entry = status.entries[0]
    assert entry.state is CoverageState.COMPLETE
    assert entry.missing_text == "None"
    assert entry.absence_text is None
    assert entry.coverage_text is None
    assert status.summary == "Every figure is complete for this window."


def test_a_missing_day_names_it_withholds_the_change_and_leaves_a_ratio_day_out() -> None:
    status = write_evidence_status(
        WEEK,
        (
            _fact(missing_dates=(date(2026, 9, 13),)),
            CoverageFact("ad_spend", "Ad spend", "Delivery"),
        ),
    )

    entry = status.entry("viewable_rate")
    assert entry.state is CoverageState.MISSING_DAYS
    assert entry.withheld
    assert entry.missing_dates == (date(2026, 9, 13),)
    assert entry.missing_text == "Sep 13"
    assert entry.absence_text == "1 day not yet sent."
    assert entry.coverage_text == "6 of 7 days"
    assert entry.reason == (
        "Covers Sep 9\u201312 and 14\u201315 (6 of 7 days): the missing day is left out of "
        "both parts."
    )
    assert entry.recovery == (
        "The figure and its change update by themselves when the day arrives."
    )
    assert status.summary == (
        "Viewable rate is partly reported for this window. It shows what arrived, without a "
        "change, until the rest arrives. Every other figure is complete."
    )


def test_an_additive_missing_day_covers_the_days_that_arrived_without_a_ratio_clause() -> None:
    entry = write_evidence_status(
        WEEK,
        (
            CoverageFact(
                "ad_spend",
                "Ad spend",
                "Delivery",
                missing_dates=(date(2026, 9, 14), date(2026, 9, 15)),
            ),
        ),
    ).entries[0]

    assert entry.absence_text == "2 days not yet sent."
    assert entry.coverage_text == "5 of 7 days"
    assert entry.missing_text == "Sep 14\u201315"
    assert entry.reason == "Covers Sep 9\u201313 (5 of 7 days)."
    assert entry.recovery.endswith("when the days arrive.")


def test_a_partial_day_counts_the_entities_that_reported_and_a_ratio_leaves_it_out() -> None:
    day = date(2026, 9, 11)
    status = write_evidence_status(
        RETAIL_WEEK,
        (
            CoverageFact(
                "customer_returns",
                "Customer returns",
                "Returns",
                partial_dates=(day,),
                partial_entities=("Summit Pack 28 L",),
                entity_word="products",
            ),
            CoverageFact(
                "return_rate",
                "Return rate",
                "Returns",
                CoverageKind.RATIO,
                partial_dates=(day,),
                partial_entities=("Summit Pack 28 L",),
                entity_word="products",
            ),
        ),
    )

    count = status.entry("customer_returns")
    assert count.state is CoverageState.PARTIAL_DAYS
    assert count.absence_text == "1 partial day."
    assert count.coverage_text == "Sep 11 counts the products that reported"
    assert count.missing_text == "Sep 11 (Summit Pack 28 L)"
    assert count.reason == "Shows the products that reported on Sep 11."
    ratio = status.entry("return_rate")
    assert ratio.absence_text == "1 partial day."
    assert ratio.coverage_text == "Sep 5\u201310, 6 of 7 days"
    assert ratio.reason == (
        "Covers Sep 5\u201310 (6 of 7 days): a partly reported day is left out of both parts."
    )
    assert status.summary.startswith("Customer returns and Return rate are partly reported")
    assert "Every other figure" not in status.summary


def test_an_unavailable_figure_carries_its_served_reason_and_days() -> None:
    entry = write_evidence_status(
        WEEK,
        (
            _fact(
                missing_dates=(date(2026, 9, 10),),
                unavailable=("No publication has arrived.", "Try again after the next delivery."),
            ),
        ),
    ).entries[0]

    assert entry.state is CoverageState.UNAVAILABLE
    assert entry.missing_text == "Sep 10"
    assert entry.reason == "No publication has arrived."
    assert entry.recovery == "Try again after the next delivery."
    assert entry.absence_text is None


def test_the_block_round_trips_and_refuses_a_forged_entry() -> None:
    status = write_evidence_status(WEEK, (_fact(missing_dates=(date(2026, 9, 13),)),))
    payload = evidence_status_payload(status)
    assert payload is not None

    assert evidence_status_from_payload(payload) == status
    assert evidence_status_from_payload(None) is None
    entry = cast("list[dict[str, object]]", payload["entries"])[0]
    forgeries: tuple[dict[str, object], ...] = (
        {**payload, "version": 1},
        {**payload, "extra": True},
        {**payload, "entries": [{**entry, "absence_text": None}]},
        {**payload, "entries": [{**entry, "missing_dates": ["2026-09-20"]}]},
        {**payload, "entries": [{**entry, "state": "complete"}]},
        {**payload, "entries": [entry, entry]},
    )
    for forged in forgeries:
        with pytest.raises(ValueError):
            evidence_status_from_payload(forged)


def _row(index: int) -> CampaignPortfolioRow:
    return portfolio_row(
        campaign_display_signature(f"Fictional campaign {index}"),
        CampaignSelectionCapability(f"selection-{index}", datetime(2026, 10, 1, tzinfo=UTC)),
        WEEK,
        Decimal(10 + index),
    )


def _portfolio_with_missing_spend_day() -> CampaignPortfolioArtifact:
    base = complete_portfolio(WEEK, (_row(0), _row(1)))
    return with_missing_day(base, CampaignPortfolioMetric.SPEND, date(2026, 9, 13))


def test_a_complete_portfolio_serves_every_figure_complete_with_dictionary_labels() -> None:
    artifact = replace(
        complete_portfolio(WEEK, (_row(0),)),
        evidence_status=campaign_portfolio_evidence_status(WEEK, LABELS),
    )
    status = artifact.evidence_status
    assert status is not None

    assert tuple(entry.slot for entry in status.entries) == (
        "spend",
        "purchases",
        "impressions",
        "portfolio_roas_attributed_14d",
        "attributed_roas",
        "ctr",
    )
    assert status.entry("spend").label == "Ad spend"
    assert all(entry.state is CoverageState.COMPLETE for entry in status.entries)
    assert (
        campaign_portfolio_artifact_from_payload(campaign_portfolio_artifact_payload(artifact))
        == artifact
    )


def test_a_missing_day_withholds_the_figure_change_on_every_surface_and_round_trips() -> None:
    artifact = _portfolio_with_missing_spend_day()
    payload = campaign_portfolio_artifact_payload(artifact)

    total = cast("list[dict[str, dict[str, object]]]", payload["totals"])[0]
    assert total["absolute_change"] is None
    assert total["change"] is None
    assert total["percentage_change"] is None
    assert total["current"]["value"] is not None
    assert campaign_portfolio_artifact_from_payload(payload) == artifact


def test_a_withheld_change_must_match_the_coverage_block() -> None:
    artifact = _portfolio_with_missing_spend_day()
    status = artifact.evidence_status
    assert status is not None
    with pytest.raises(ValueError, match="withheld exactly"):
        replace(artifact, evidence_status=campaign_portfolio_evidence_status(WEEK, LABELS))
    with pytest.raises(ValueError, match="requires its coverage block"):
        replace(artifact, evidence_status=None)
    complete = replace(
        complete_portfolio(WEEK, (_row(0),)),
        evidence_status=campaign_portfolio_evidence_status(WEEK, LABELS),
    )
    with pytest.raises(ValueError, match="withheld exactly"):
        replace(complete, evidence_status=status)
    with pytest.raises(ValueError, match="another window"):
        replace(
            complete,
            evidence_status=campaign_portfolio_evidence_status(
                DateRange(date(2026, 9, 2), date(2026, 9, 8)), LABELS
            ),
        )


def test_a_format_7_portfolio_body_without_the_block_is_refused_and_format_11_decodes() -> None:
    artifact = complete_portfolio(WEEK, (_row(0),))
    payload = campaign_portfolio_artifact_payload(artifact)
    # Positive control: the current body decodes with its coverage block.
    assert campaign_portfolio_artifact_from_payload(payload) == artifact
    assert artifact.evidence_status is not None

    without_key = {key: value for key, value in payload.items() if key != "evidence_status"}
    with pytest.raises(ValueError) as missing:
        campaign_portfolio_artifact_from_payload(without_key)
    assert str(missing.value) == "campaign portfolio artifact fields are not exact."
    with pytest.raises(ValueError) as null_on_complete:
        campaign_portfolio_artifact_from_payload({**payload, "evidence_status": None})
    assert str(null_on_complete.value) == (
        "a complete campaign portfolio body carries its coverage block."
    )
    with pytest.raises(ValueError):
        campaign_portfolio_artifact_from_payload(
            {
                **payload,
                "evidence_status": evidence_status_payload(
                    campaign_portfolio_evidence_status(WEEK, LABELS)
                ),
                "outcome": "empty",
            }
        )


def test_campaign_detail_coverage_names_real_missing_dates_for_a_refused_slot() -> None:
    hole = (date(2026, 9, 12), date(2026, 9, 13))
    statuses = tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(
                status.slot,
                status.role,
                CampaignEvidenceStatusCondition.COVERAGE_HOLE,
                missing_dates=hole,
            )
        )
        if (status.slot, status.role) == ("clicks", "current")
        else status
        for status in campaign_delivery_accepted_statuses()
    )

    status: EvidenceStatus = campaign_detail_evidence_status(WEEK, statuses, LABELS)

    clicks = status.entry("clicks")
    assert clicks.state is CoverageState.UNAVAILABLE
    assert clicks.label == "Clicks"
    assert clicks.role_label == "Delivery, this window"
    assert clicks.missing_text == "Sep 12\u201313"
    assert clicks.reason.startswith("Published coverage for this window has gaps")
    assert status.entry("clicks", "prior").state is CoverageState.COMPLETE
    assert status.entry("ad_spend", "prior_daily").role_label == (
        "Delivery, daily series, prior window"
    )
    assert status.summary.startswith("Clicks can't be shown for this window")


def test_campaign_detail_coverage_never_repeats_a_figure_and_role_pair() -> None:
    """The attributed and delivery ad spend slots share a label; the role names the family."""
    attributed = tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(slot, role, CampaignEvidenceStatusCondition.AVAILABLE)
        )
        for slot, role in (
            ("purchases", "current"),
            ("attributed_roas", "current"),
            ("spend", "current"),
            ("purchase_series", "daily"),
            ("prior_purchases", "prior"),
            ("prior_attributed_roas", "prior"),
            ("prior_spend", "prior"),
        )
    )
    status = campaign_detail_evidence_status(
        WEEK, attributed + campaign_delivery_accepted_statuses(), LABELS
    )

    pairs = [(entry.label, entry.role_label) for entry in status.entries]

    assert len(pairs) == len(set(pairs)) == 31
    assert status.entry("spend").role_label == "Attributed outcomes, this window"
    assert status.entry("prior_spend", "prior").role_label == "Attributed outcomes, prior window"
    assert status.entry("ad_spend").role_label == "Delivery, this window"
    assert status.entry("ad_spend", "prior").role_label == "Delivery, prior window"
    assert status.entry("spend").label == status.entry("ad_spend").label


def test_campaign_detail_coverage_reads_not_sent_only_where_no_publication_arrived() -> None:
    refused = {
        ("ad_spend", "current"): CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION,
        ("ad_spend", "prior"): CampaignEvidenceStatusCondition.MISSING_COMPARATOR_PUBLICATION,
        ("clicks", "current"): CampaignEvidenceStatusCondition.SUPPRESSED,
        ("ctr", "current"): CampaignEvidenceStatusCondition.SERIES_TOTAL_DIVERGENCE,
    }
    statuses = tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(status.slot, status.role, refused[key])
        )
        if (key := (status.slot, status.role)) in refused
        else status
        for status in campaign_delivery_accepted_statuses()
    )

    status = campaign_detail_evidence_status(WEEK, statuses, LABELS)

    for slot, role in (("ad_spend", "current"), ("ad_spend", "prior")):
        entry = status.entry(slot, role)
        assert entry.state is CoverageState.UNAVAILABLE
        assert entry.missing_dates == ()
        assert entry.missing_text == "Not sent"
    # A refusal that isn't day-shaped, and a complete figure, still read "None".
    assert status.entry("clicks").missing_text == "None"
    assert status.entry("ctr").missing_text == "None"
    assert status.entry("impressions").missing_text == "None"
    assert status.entry("impressions").reason == "Every day of this window has arrived."
    assert status.entry("impressions", "prior").reason == (
        "Every day of the prior window has arrived."
    )
    assert status.entry("impressions", "prior_daily").reason == (
        "Every day of the prior window has arrived."
    )


def test_only_an_unavailable_figure_with_no_named_days_is_unsent() -> None:
    with pytest.raises(ValueError, match="unsent"):
        _fact(unsent=True)
    with pytest.raises(ValueError, match="unsent"):
        _fact(
            unsent=True,
            missing_dates=(date(2026, 9, 10),),
            unavailable=("No publication has arrived.", "Try again after the next delivery."),
        )


def test_uncovered_days_reads_holes_and_short_coverage() -> None:
    assert uncovered_days(
        WEEK,
        (DateRange(date(2026, 9, 9), date(2026, 9, 14)),),
        (DateRange(date(2026, 9, 11), date(2026, 9, 11)),),
    ) == (date(2026, 9, 11), date(2026, 9, 15))


def test_an_undefined_ratio_under_a_withheld_column_has_no_change_to_withhold() -> None:
    """A row's CTR over zero impressions is typed absent (ADR-087), even while CTR is partial."""
    base = complete_portfolio(WEEK, (_row(0),))
    undefined_ctr = replace(
        base,
        rows=tuple(
            replace(
                row,
                comparisons=tuple(
                    replace(
                        item,
                        current=replace(item.current, value=None),
                        absolute_change=None,
                        percentage_change=None,
                        change=None,
                    )
                    if item.metric is CampaignPortfolioMetric.CTR
                    else item
                    for item in row.comparisons
                ),
            )
            for row in base.rows
        ),
    )

    artifact = with_missing_day(undefined_ctr, CampaignPortfolioMetric.CTR, date(2026, 9, 13))

    assert artifact.rows[0].comparison_for(CampaignPortfolioMetric.CTR).withheld is False
    assert (
        campaign_portfolio_artifact_from_payload(campaign_portfolio_artifact_payload(artifact))
        == artifact
    )
