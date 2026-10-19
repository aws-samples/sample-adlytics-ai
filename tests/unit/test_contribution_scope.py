# SPDX-License-Identifier: MIT-0
"""C1: the contribution card states which campaigns it reads and how spend moved outside them.

Cards are filled per served (sort, segment) segment (design §4.5). Without the scope sentence
the model reads 50 or 10 of 60 campaigns as the portfolio (W7-c).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
    portfolio_totals,
)

from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignSelectionCapability,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioOutcome,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_population,
    campaign_portfolio_segment,
    campaign_sort_label,
    campaigns_in_view_sentence,
    portfolio_spend_change_split,
)
from domain.presentation.insights import InsightSlotId
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
SCOPE = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")}))


def _population(count: int, total_spend: tuple[str, str]) -> CampaignPortfolioArtifact:
    rows = tuple(
        portfolio_row(
            campaign_display_signature(f"Fictional Campaign {index:03d}"),
            CampaignSelectionCapability(f"baked:{index:05d}", datetime(2027, 1, 1, tzinfo=UTC)),
            PERIOD,
            Decimal(1300 + index),
            prior_spend=Decimal(1000),
            roas=(Decimal("2.5"), Decimal("2.4") if index % 2 else Decimal("2.8")),
        )
        for index in range(count)
    )
    totals = portfolio_totals(PERIOD, spend=(Decimal(total_spend[0]), Decimal(total_spend[1])))
    return complete_portfolio(PERIOD, rows, totals=totals)


def _segment(  # noqa: PLR0913, PLR0917 -- one segment's four coordinates
    artifact: CampaignPortfolioArtifact,
    field: CampaignPortfolioSortField,
    direction: str,
    index: int,
) -> CampaignPortfolioArtifact:
    segment = campaign_portfolio_segment(artifact, CampaignPortfolioSort(field, direction), index)
    assert segment is not None
    return segment


def _context(artifact: CampaignPortfolioArtifact, slot: InsightSlotId) -> dict[str, str]:
    derivation, _ = portfolio_insight_request_derivation(
        artifact=artifact, scope=SCOPE, contracts=CONTRACTS
    )
    request = next(item for item in derivation.requests if item.key.slot_id is slot)
    return {entry.name: entry.value for entry in request.context}


SPEND = CampaignPortfolioSortField.SPEND


@pytest.mark.parametrize(
    ("field", "direction", "index", "expected"),
    [
        (
            SPEND,
            "desc",
            0,
            "These are the 50 campaigns ranked 1 to 50 by ad spend, highest first in this "
            "window, of 60 in the portfolio",
        ),
        (
            CampaignPortfolioSortField.ATTRIBUTED_ROAS,
            "desc",
            1,
            "These are the 10 campaigns ranked 51 to 60 by attributed ROAS, highest first in "
            "this window, of 60 in the portfolio",
        ),
        (
            CampaignPortfolioSortField.NAME,
            "asc",
            0,
            "These are the 50 campaigns ranked 1 to 50 by campaign name, A to Z in this "
            "window, of 60 in the portfolio",
        ),
    ],
)
def test_the_contribution_card_names_its_campaigns_and_their_order(  # noqa: PLR0913, PLR0917
    field: CampaignPortfolioSortField, direction: str, index: int, expected: str
) -> None:
    artifact = _segment(_population(60, ("85000", "60000")), field, direction, index)
    assert _context(artifact, InsightSlotId.CONTRIBUTION)["campaigns in view"] == expected


def test_one_segment_holding_every_campaign_says_so() -> None:
    artifact = _segment(_population(30, ("33000", "30000")), SPEND, "desc", 0)
    context = _context(artifact, InsightSlotId.CONTRIBUTION)
    assert context["campaigns in view"] == (
        "These are all 30 campaigns in the portfolio, ranked by ad spend, highest first"
    )


def test_the_contribution_card_states_spend_in_and_outside_the_view() -> None:
    # The 50 in view move +310 to +359 (sum 16,725); the total moves +25,000: +8,275 outside.
    artifact = _segment(_population(60, ("85000", "60000")), SPEND, "desc", 0)
    context = _context(artifact, InsightSlotId.CONTRIBUTION)
    assert context["spend change in view"] == "16,725.00 USD"
    assert context["spend change outside the view"] == "8,275.00 USD"
    assert context["share of the portfolio change"] == "67% of the portfolio's spend change"
    assert context["direction counts in view"] == "Among these 50: 50 rose, 0 fell, 0 were flat"


def test_the_share_is_withheld_when_the_outside_move_opposes_the_total() -> None:
    # In view +16,725, total +1,000: outside is -15,725, so a share would be a gross reading.
    artifact = _segment(_population(60, ("61000", "60000")), SPEND, "desc", 0)
    context = _context(artifact, InsightSlotId.CONTRIBUTION)
    assert context["spend change outside the view"] == "-15,725.00 USD"
    assert "share of the portfolio change" not in context


def test_the_divergence_card_gets_the_scope_sentence_only() -> None:
    artifact = _segment(_population(60, ("85000", "60000")), SPEND, "desc", 1)
    divergence = _context(artifact, InsightSlotId.DIVERGENCE)
    assert divergence["campaigns in view"] == (
        "These are the 10 campaigns ranked 51 to 60 by ad spend, highest first in this "
        "window, of 60 in the portfolio"
    )
    for absent in (
        "direction counts in view",
        "spend change in view",
        "spend change outside the view",
        "share of the portfolio change",
    ):
        assert absent not in divergence


@pytest.mark.parametrize(
    ("field", "direction", "label"),
    [
        (SPEND, "asc", "ad spend, lowest first"),
        (CampaignPortfolioSortField.PURCHASES, "desc", "attributed purchases, highest first"),
        (CampaignPortfolioSortField.CTR, "desc", "click-through rate, highest first"),
        (CampaignPortfolioSortField.NAME, "desc", "campaign name, Z to A"),
    ],
)
def test_sort_labels_read_naturally_for_each_field(
    field: CampaignPortfolioSortField, direction: str, label: str
) -> None:
    assert campaign_sort_label(CampaignPortfolioSort(field, direction)) == label


def _empty() -> CampaignPortfolioArtifact:
    return CampaignPortfolioArtifact(
        CampaignPortfolioOutcome.EMPTY,
        PERIOD,
        DateRange(date(2026, 8, 6), date(2026, 8, 12)),
        CampaignPortfolioSort(SPEND, "desc"),
        (),
        0,
        None,
        (),
        None,
        (CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )


def test_a_portfolio_without_pagination_reads_its_own_population() -> None:
    """Mutation review: pagination is absent only when no rows are served (ADR-071)."""
    empty = _empty()
    assert campaign_population(empty) == 0
    assert campaigns_in_view_sentence(empty) == (
        "These are all 0 campaigns in the portfolio, ranked by ad spend, highest first"
    )
    # Positive control: a segment reads the population from its pagination record.
    assert (
        campaign_population(_segment(_population(60, ("85000", "60000")), SPEND, "desc", 1)) == 60
    )


def test_a_portfolio_without_totals_has_no_spend_split() -> None:
    """Mutation review: an empty outcome carries no spend total, so no split is stated."""
    assert portfolio_spend_change_split(_empty()) is None
    # Positive control: the complete portfolio states its split.
    assert portfolio_spend_change_split(_population(12, ("13000", "12000"))) is not None


def test_sort_words_compare_the_direction_by_value() -> None:
    """Mutation review: a parsed direction is an equal string, not the literal object."""
    ascending = "".join(("a", "sc"))
    assert campaign_sort_label(
        CampaignPortfolioSort(CampaignPortfolioSortField.NAME, ascending)
    ) == ("campaign name, A to Z")
    assert campaign_sort_label(CampaignPortfolioSort(SPEND, ascending)) == "ad spend, lowest first"
