# SPDX-License-Identifier: MIT-0
"""Design §3.3 pre-landing check 2: the four zero-denominator serve shapes, in-process.

Each shape is rendered from the committed fixtures through the publisher's renderer. A ratio
over a zero denominator is typed absent (ADR-087), never 0 and never infinite; an additive
metric over nothing is 0.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from campaign_portfolio_support import ONE_CHANNEL_FILTER
from fixture_artifacts_support import ROOT, fixture_catalog, render_campaign_artifacts

from adapters.campaign_artifact_renderer import CampaignArtifactRenderResult
from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.fixture_generation.shape import Pause
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioMetric,
    CampaignPortfolioRow,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
)
from domain.presentation.metric_focus import (
    CAMPAIGN_PORTFOLIO_FOCUS_METRICS,
    campaign_portfolio_focus_ingredients,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086", "ADR-087")]

_DENOMINATOR = {
    CampaignPortfolioMetric.ATTRIBUTED_ROAS: CampaignPortfolioMetric.SPEND,
}
"""Each served row ratio whose denominator the row also serves."""


def _window(end: date, width: int) -> tuple[DateRange, DateRange]:
    current = DateRange(end - timedelta(days=width - 1), end)
    prior = DateRange(current.start - timedelta(days=width), current.start - timedelta(days=1))
    return current, prior


def _shapes() -> dict[str, tuple[DateRange, DateRange, str]]:
    shape = fixture_catalog().shape
    anchor = shape.event_anchor
    pause = next(item for item in shape.events if isinstance(item, Pause)).campaign
    return {
        "1-prior-zero": (*_window(anchor, 1), pause),
        "2-both-zero": (*_window(anchor - timedelta(days=1), 1), pause),
        "3-current-zero": (*_window(anchor - timedelta(days=2), 1), pause),
        "4-launch-14d": (*_window(anchor, 14), shape.launch.campaign),
    }


_RENDERED: dict[str, CampaignArtifactRenderResult] = {}


def _rendered(name: str) -> CampaignArtifactRenderResult:
    if name not in _RENDERED:
        period, prior, _key = _shapes()[name]
        rendered = render_campaign_artifacts(period, prior)
        assert rendered is not None
        _RENDERED[name] = rendered
    return _RENDERED[name]


def _label(key: str) -> str:
    return next(item for item in fixture_catalog().campaigns if item.key == key).names[-1].value


def _row(rendered: CampaignArtifactRenderResult, key: str) -> CampaignPortfolioRow:
    label = _label(key)
    return next(item for item in rendered.portfolio.rows if item.signature.effective_name == label)


def _zero_sides(row: CampaignPortfolioRow) -> list[tuple[CampaignPortfolioMetric, str]]:
    by_metric = {item.metric: item for item in row.comparisons}
    return [
        (metric, side)
        for metric, denominator in _DENOMINATOR.items()
        for side in ("current", "prior")
        if Decimal(getattr(by_metric[denominator], side).number) == 0
    ]


@pytest.mark.parametrize("name", sorted(_shapes()))
def test_2_a_zero_denominator_ratio_is_typed_absent_and_serves_without_error(name: str) -> None:
    rendered = _rendered(name)
    portfolio = rendered.portfolio
    assert portfolio.outcome.value == "complete"
    for metric in CAMPAIGN_PORTFOLIO_FOCUS_METRICS:
        campaign_portfolio_focus_ingredients(portfolio, metric)
    row = _row(rendered, _shapes()[name][2])
    by_metric = {item.metric: item for item in row.comparisons}
    zero_sides = _zero_sides(row)
    assert zero_sides, name  # the shape reaches a zero denominator
    for metric, side in zero_sides:
        assert getattr(by_metric[metric], side).value is None, (name, metric, side)
        assert by_metric[metric].absolute_change is None
        assert by_metric[metric].percentage_change is None
    # Additive control: zero spend is a measured 0, displayed 0.00, never typed absent.
    spend = by_metric[CampaignPortfolioMetric.SPEND]
    for side, payload in (("current", spend.current), ("prior", spend.prior)):
        if (CampaignPortfolioMetric.ATTRIBUTED_ROAS, side) in zero_sides:
            assert payload.value == 0
    for item in row.comparisons:
        if item.prior.value is not None and Decimal(item.prior.value) == 0:
            assert item.percentage_change is None
    # The payload round-trips with the nulls in place, served as a one-channel sibling: a
    # served body always names its channel filter (render format 11).
    served = replace(portfolio, channel_filter=ONE_CHANNEL_FILTER)
    payload = campaign_portfolio_artifact_payload(served)
    assert campaign_portfolio_artifact_from_payload(json.loads(json.dumps(payload))) == served


@pytest.mark.parametrize("name", sorted(_shapes()))
def test_2_a_ratio_with_a_real_denominator_still_serves_its_value(name: str) -> None:
    """Positive control: every other campaign's ROAS and CTR in the same artifact is a number."""
    rendered = _rendered(name)
    key = _shapes()[name][2]
    others = [row for row in rendered.portfolio.rows if row.signature.effective_name != _label(key)]
    assert len(others) >= 20
    for row in others:
        for item in row.comparisons:
            assert item.current.value is not None and item.prior.value is not None
            assert item.absolute_change is not None


def test_2_an_undefined_ratio_never_reaches_the_model_as_a_zero() -> None:
    rendered = _rendered("2-both-zero")
    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    scope = AuthorizedAnalyticalScope(
        frozenset({load_commerce_identity_bridge(ROOT).commerce_scope})
    )
    derivation, _movers = portfolio_insight_request_derivation(
        artifact=rendered.portfolio, scope=scope, contracts=contracts
    )
    label = _label(_shapes()["2-both-zero"][2])
    divergence = derivation.findings_by_slot.get(
        next(slot for slot in derivation.findings_by_slot if slot.value == "divergence")
    )
    assert divergence is not None
    assert all(finding.name != label for finding in divergence)
    for request in derivation.requests:
        rendered_context = json.dumps([str(entry) for entry in request.context])
        assert "None" not in rendered_context


def test_2_shape_4_the_launch_campaign_has_no_detail_as_a_closed_outcome() -> None:
    """A 14D prior with no conversion rows refuses the exact-row detail answer (closed)."""
    rendered = _rendered("4-launch-14d")
    key = _shapes()["4-launch-14d"][2]
    assert _row(rendered, key).comparisons  # the row itself is served
    assert sum(detail is not None for detail in rendered.details) == len(rendered.details) - 1
    named = [detail.selection.effective_name for detail in rendered.details if detail is not None]
    assert _label(key) not in named
