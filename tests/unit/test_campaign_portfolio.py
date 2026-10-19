# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913, PLR0917
"""Campaign Portfolio V2 parent contract invariants and exact codec coverage."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from campaign_portfolio_support import (
    LABELS,
    ONE_CHANNEL_FILTER,
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
)
from change_support import SEMANTICS

from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
    CampaignPortfolioArtifact,
    CampaignPortfolioComparison,
    CampaignPortfolioInsufficiencyReason,
    CampaignPortfolioMetric,
    CampaignPortfolioMetricValue,
    CampaignPortfolioOutcome,
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    CampaignPortfolioTrend,
    CampaignPortfolioTrendPoint,
    build_campaign_portfolio,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_comparison,
    campaign_portfolio_evidence_status,
    campaign_portfolio_segment,
    order_campaign_portfolio_rows,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.currency_display import without_display_values
from domain.presentation.insights import InsightFindingKind, InsightSlotId, OperandBasis
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 3))
PRIOR = DateRange(date(2026, 7, 29), date(2026, 7, 31))
USD = Units(UnitKind.CURRENCY, "USD")
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)


def _build_served(**fields: Any) -> CampaignPortfolioArtifact:
    """Build a portfolio as a served render-format-11 body.

    It is a one-channel sibling, with its coverage block when complete, because the decoder
    refuses the retired forms.
    """
    fields.setdefault("channel_filter", ONE_CHANNEL_FILTER)
    if fields.get("rows") and not fields.get("reason_codes") and "evidence_status" not in fields:
        fields["evidence_status"] = campaign_portfolio_evidence_status(fields["period"], LABELS)
    return build_campaign_portfolio(**fields)


def _value(
    value: str | int, units: Units, period: DateRange, grain: object
) -> CampaignPortfolioMetricValue:
    numeric = value if isinstance(value, int) else Decimal(value)
    return CampaignPortfolioMetricValue(
        numeric,
        units,
        MeasurementBasis.AD_ATTRIBUTED,
        period,
        grain,  # type: ignore[arg-type]
    )


def _comparison(
    metric: CampaignPortfolioMetric,
    current: str | int,
    prior: str | int,
    units: Units,
    grain: object,
) -> CampaignPortfolioComparison:
    # Every two-sided comparison carries its governed change block (ADR-121).
    return campaign_portfolio_comparison(
        metric, _value(current, units, PERIOD, grain), _value(prior, units, PRIOR, grain), SEMANTICS
    )


def _row(index: int) -> CampaignPortfolioRow:
    signature = SafeCampaignDisplaySignature(
        effective_name=f"Campaign {index:03d}",
        channel="Fictional channel",
        marketplace="United States",
        platform="Amazon Ads",
        effective_name_status=EffectiveNameStatus.AVAILABLE,
        effective_name_valid_from=date(2026, 1, 1),
    )
    comparisons = (
        _comparison(
            CampaignPortfolioMetric.SPEND, str(index + 10), "5", USD, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
        _comparison(
            CampaignPortfolioMetric.ATTRIBUTED_ROAS, "2.5", "2", RATIO, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
        _comparison(
            CampaignPortfolioMetric.PURCHASES, index + 1, 1, COUNT, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
        _comparison(
            CampaignPortfolioMetric.CTR, "0.25", "0.2", RATIO, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
    )
    return CampaignPortfolioRow(
        signature,
        CampaignSelectionCapability(f"selection-{index:03d}", datetime(2026, 9, 1, tzinfo=UTC)),
        comparisons,
        ("fresh",),
    )


def _totals() -> tuple[CampaignPortfolioComparison, ...]:
    return (
        _comparison(
            CampaignPortfolioMetric.SPEND, "1000", "900", USD, CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
        ),
        _comparison(
            CampaignPortfolioMetric.PURCHASES, 100, 90, COUNT, CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
        ),
        _comparison(
            CampaignPortfolioMetric.IMPRESSIONS, 10000, 9000, COUNT, CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
        ),
        _comparison(
            CampaignPortfolioMetric.PORTFOLIO_ROAS,
            "2.5",
            "2",
            RATIO,
            CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
        ),
    )


@pytest.mark.covers("ADR-017")
def test_portfolio_preserves_governed_default_roas_and_bounds_rows_after_totals() -> None:
    trend = CampaignPortfolioTrend(
        PERIOD,
        tuple(
            CampaignPortfolioTrendPoint(
                PERIOD.start + timedelta(days=offset), 10, Decimal("2.5"), 1
            )
            for offset in range(PERIOD.days)
        ),
        USD,
        RATIO,
        COUNT,
        MeasurementBasis.AD_ATTRIBUTED,
    )
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=tuple(_row(index) for index in range(CAMPAIGN_PORTFOLIO_MAX_ROWS + 1)),
        totals=_totals(),
        trend=trend,
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )

    assert artifact.pagination is not None
    assert artifact.pagination.segment_size == CAMPAIGN_PORTFOLIO_MAX_ROWS
    assert artifact.pagination.segment_index == 0
    assert artifact.pagination.segment_count == 2
    assert artifact.total_count == CAMPAIGN_PORTFOLIO_MAX_ROWS + 1
    assert len(artifact.rows) == CAMPAIGN_PORTFOLIO_MAX_ROWS + 1
    assert len(artifact.roster) == artifact.total_count
    assert tuple(entry.position for entry in artifact.roster) == tuple(range(artifact.total_count))
    assert artifact.totals[-1].metric is CampaignPortfolioMetric.PORTFOLIO_ROAS
    assert artifact.totals[-1].current.units is RATIO
    assert artifact.trend is not None
    assert tuple(point.day for point in artifact.trend.points) == tuple(
        PERIOD.start + timedelta(days=offset) for offset in range(PERIOD.days)
    )
    assert (
        campaign_portfolio_artifact_from_payload(campaign_portfolio_artifact_payload(artifact))
        == artifact
    )

    legacy_payload = campaign_portfolio_artifact_payload(artifact)
    legacy_payload["roster"] = [
        {
            "channel": entry.channel,
            "marketplace": entry.marketplace,
            "name": entry.name,
            "position": entry.position,
        }
        for entry in artifact.roster
    ]
    assert campaign_portfolio_artifact_from_payload(legacy_payload) == artifact


def _trend() -> CampaignPortfolioTrend:
    return CampaignPortfolioTrend(
        PERIOD,
        tuple(
            CampaignPortfolioTrendPoint(
                PERIOD.start + timedelta(days=offset), 10, Decimal("2.5"), 1
            )
            for offset in range(PERIOD.days)
        ),
        USD,
        RATIO,
        COUNT,
        MeasurementBasis.AD_ATTRIBUTED,
    )


def test_portfolio_codec_round_trips_signed_row_delta_without_accepting_negative_raw_metrics() -> (
    None
):
    row = _row(0)
    decreasing_spend = _comparison(
        CampaignPortfolioMetric.SPEND,
        "5",
        "10",
        USD,
        CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    )
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(
            CampaignPortfolioRow(
                row.signature,
                row.capability,
                (decreasing_spend, *row.comparisons[1:]),
                row.freshness_qualifiers,
            ),
        ),
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )

    payload = campaign_portfolio_artifact_payload(artifact)
    comparison = payload["rows"][0]["comparisons"][0]  # type: ignore[index]
    assert comparison["absolute_change"] == "-5"  # type: ignore[index]
    # A current comparison carries its change block in place of the percentage (ADR-121).
    assert "percentage_change" not in comparison  # type: ignore[operator]
    assert comparison["change"]["direction"] == "down"  # type: ignore[index]
    assert campaign_portfolio_artifact_from_payload(payload) == artifact

    comparison["current"]["value"] = "-5"  # type: ignore[index]
    with pytest.raises(ValueError, match="cannot be negative"):
        campaign_portfolio_artifact_from_payload(payload)


def test_portfolio_model_refuses_payload_fields_for_empty_and_insufficient_outcomes() -> None:
    evidence = (CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),)
    sort = CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
    with pytest.raises(ValueError, match="empty campaign portfolio fields are contradictory"):
        CampaignPortfolioArtifact(
            CampaignPortfolioOutcome.EMPTY,
            PERIOD,
            PRIOR,
            sort,
            (_row(0),),
            1,
            None,
            _totals(),
            _trend(),
            evidence,
        )
    with pytest.raises(
        ValueError, match="insufficient campaign portfolio fields are contradictory"
    ):
        CampaignPortfolioArtifact(
            CampaignPortfolioOutcome.INSUFFICIENT,
            PERIOD,
            PRIOR,
            sort,
            (_row(0),),
            1,
            None,
            _totals(),
            _trend(),
            evidence,
            (CampaignPortfolioInsufficiencyReason.INCOMPATIBLE_SCOPE,),
        )


def _first_row_comparison_payload(payload: dict[str, object]) -> dict[str, object]:
    rows = cast("list[object]", payload["rows"])
    row = cast("dict[str, object]", rows[0])
    comparisons = cast("list[object]", row["comparisons"])
    return cast("dict[str, object]", comparisons[0])


def _first_total_comparison_payload(payload: dict[str, object]) -> dict[str, object]:
    totals = cast("list[object]", payload["totals"])
    return cast("dict[str, object]", totals[0])


def _forge_delta(payload: dict[str, object]) -> None:
    _first_row_comparison_payload(payload)["absolute_change"] = "-4"


def _forge_row_grain(payload: dict[str, object]) -> None:
    current = cast("dict[str, object]", _first_row_comparison_payload(payload)["current"])
    current["grain"] = ["period", "marketplace"]


def _forge_total_unit(payload: dict[str, object]) -> None:
    current = cast("dict[str, object]", _first_total_comparison_payload(payload)["current"])
    current["units"] = {"kind": "count", "currency_code": None}


def _forge_prior_period(payload: dict[str, object]) -> None:
    prior = cast("dict[str, object]", _first_row_comparison_payload(payload)["prior"])
    prior["period"] = {"start": "2026-07-28", "end": "2026-07-30"}


@pytest.mark.parametrize(
    "mutate",
    (_forge_delta, _forge_row_grain, _forge_total_unit, _forge_prior_period),
)
def test_portfolio_codec_rejects_forged_delta_and_metric_semantic_drift(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(_row(0),),
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )
    payload = campaign_portfolio_artifact_payload(artifact)
    mutate(payload)

    with pytest.raises(ValueError):
        campaign_portfolio_artifact_from_payload(payload)


def _insufficient_artifact(
    reason_codes: tuple[CampaignPortfolioInsufficiencyReason, ...],
) -> CampaignPortfolioArtifact:
    return CampaignPortfolioArtifact(
        CampaignPortfolioOutcome.INSUFFICIENT,
        PERIOD,
        PRIOR,
        CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        (),
        0,
        None,
        (),
        None,
        (CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
        reason_codes,
    )


@pytest.mark.parametrize(
    "reason_codes",
    (
        cast("tuple[CampaignPortfolioInsufficiencyReason, ...]", ("unknown_reason",)),
        (
            CampaignPortfolioInsufficiencyReason.INCOMPATIBLE_SCOPE,
            CampaignPortfolioInsufficiencyReason.MISSING_CATALOG,
        ),
        (
            CampaignPortfolioInsufficiencyReason.MISSING_CATALOG,
            CampaignPortfolioInsufficiencyReason.MISSING_CATALOG,
        ),
    ),
)
def test_portfolio_model_refuses_unknown_or_noncanonical_insufficiency_reasons(
    reason_codes: tuple[CampaignPortfolioInsufficiencyReason, ...],
) -> None:
    with pytest.raises(ValueError, match="reason codes must be unique and canonical"):
        _insufficient_artifact(reason_codes)


@pytest.mark.parametrize(
    "reason_codes",
    (
        ["unknown_reason"],
        ["incompatible_scope", "missing_catalog"],
        ["missing_catalog", "missing_catalog"],
    ),
)
def test_portfolio_codec_refuses_unknown_or_noncanonical_insufficiency_reasons(
    reason_codes: list[str],
) -> None:
    payload = campaign_portfolio_artifact_payload(
        _insufficient_artifact((CampaignPortfolioInsufficiencyReason.MISSING_CATALOG,))
    )
    payload["reason_codes"] = reason_codes

    with pytest.raises(ValueError):
        campaign_portfolio_artifact_from_payload(payload)


@pytest.mark.covers("ADR-068")
def test_portfolio_codec_requires_the_declared_unavailable_insight_roster() -> None:
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(_row(0),),
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )

    payload = campaign_portfolio_artifact_payload(artifact)
    insights = cast("list[object]", payload["insights"])
    first = cast("dict[str, object]", insights[0])
    assert tuple(slot["id"] for slot in cast("list[dict[str, object]]", insights)) == (
        "contribution",
        "divergence",
        "exposure",
        "movers",
    )
    assert first["state"] == "unavailable"
    assert first["artifact_revision"] == "unpublished"
    assert campaign_portfolio_artifact_from_payload(payload) == artifact

    del payload["insights"]
    with pytest.raises(ValueError, match="fields are not exact"):
        campaign_portfolio_artifact_from_payload(payload)


@pytest.mark.covers("ADR-068")
def test_portfolio_codec_re_attests_a_retained_body_without_declared_absences() -> None:
    """Exact readback must accept retained baked bodies whose slots declare no absence.

    Baked pointer bodies are immutable and retained revisions stay serveable to
    still-open pages; this is the exact regression behind the 2026-09-23
    ADLY-PAGE_CONTEXT-0004 campaigns 500.
    """
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(_row(0),),
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )
    payload = campaign_portfolio_artifact_payload(artifact)
    retained = {
        **payload,
        "insights": [
            {
                key: value
                for key, value in cast("dict[str, object]", slot).items()
                if key not in {"absence_class", "absence_reason"}
            }
            for slot in cast("list[object]", payload["insights"])
        ],
    }

    decoded = campaign_portfolio_artifact_from_payload(retained)

    assert all(slot.absence_class is None for slot in decoded.insights)
    assert campaign_portfolio_artifact_payload(decoded) == retained


def _row_named(
    index: int, name: str | None, channel: str, *, prior_spend: str = "5"
) -> CampaignPortfolioRow:
    """Build a row whose display identity and prior spend the test chooses."""
    base = _row(index)
    signature = SafeCampaignDisplaySignature(
        effective_name=name,
        channel=channel,
        marketplace=base.signature.marketplace,
        platform=base.signature.platform,
        effective_name_status=(
            EffectiveNameStatus.AVAILABLE if name is not None else EffectiveNameStatus.MISSING
        ),
        effective_name_valid_from=base.signature.effective_name_valid_from if name else None,
    )
    spend = _comparison(
        CampaignPortfolioMetric.SPEND,
        str(index + 10),
        prior_spend,
        USD,
        CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    )
    return CampaignPortfolioRow(
        signature,
        base.capability,
        (spend, *base.comparisons[1:]),
        base.freshness_qualifiers,
    )


def _derivation(rows: tuple[CampaignPortfolioRow, ...]) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=rows,
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )
    _, movers = portfolio_insight_request_derivation(
        artifact=artifact,
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")})),
        contracts={contract.id: contract for _, contract in load_insight_contracts(root)},
    )
    assert len(movers) == len(rows), "every row is ranked among the movers"
    names = tuple(finding.name for finding in movers)
    assert len(set(names)) == len(names), "each row carries a distinct finding name"


def _diverging_row(index: int) -> CampaignPortfolioRow:
    """Build a row whose attributed ROAS moves against the fixture portfolio direction."""
    base = _row(index)
    declining = _comparison(
        CampaignPortfolioMetric.ATTRIBUTED_ROAS, "1.5", "2", RATIO, CAMPAIGN_PORTFOLIO_ROW_GRAIN
    )
    return CampaignPortfolioRow(
        base.signature,
        base.capability,
        (base.comparisons[0], declining, *base.comparisons[2:]),
        base.freshness_qualifiers,
    )


@pytest.mark.covers("ADR-068")
def test_divergence_direction_comes_from_the_portfolio_roas_total() -> None:
    """The portfolio-level direction is the PORTFOLIO_ROAS total.

    The previous lookup used the row-grain attributed-ROAS metric, which a validated
    artifact's totals can never carry, so divergence classified as refused on every
    portfolio regardless of the data.
    """
    root = Path(__file__).resolve().parents[2]
    contracts = {contract.id: contract for _, contract in load_insight_contracts(root)}
    scope = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")}))

    def derive(rows: tuple[CampaignPortfolioRow, ...]):
        artifact = _build_served(
            period=PERIOD,
            sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
            rows=rows,
            totals=_totals(),
            trend=_trend(),
            evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
        )
        derivation, _ = portfolio_insight_request_derivation(
            artifact=artifact, scope=scope, contracts=contracts
        )
        return derivation

    # Fixture rows move with the portfolio (+0.5 ROAS, ratio grain): evaluated in full,
    # quiet with the divergence slot's own customer reason (W13) — never ineligible.
    aligned = derive((_row(0), _row(1)))
    assert InsightSlotId.DIVERGENCE not in aligned.missing_evidence_by_slot
    assert aligned.silence_reasons[InsightSlotId.DIVERGENCE] == (
        "No campaign moved against the portfolio's direction enough to call out in this view."
    )

    # One row moving materially against the portfolio direction admits the slot.
    diverging = derive((_row(0), _diverging_row(1)))
    assert InsightSlotId.DIVERGENCE in {request.key.slot_id for request in diverging.requests}


@pytest.mark.covers("ADR-076")
def test_divergence_prior_map_keeps_the_portfolio_prior_on_a_display_name_collision() -> None:
    """A campaign display-named like the portfolio operand must not overwrite its prior.

    The near-zero guard reads priors by operand name and the merge order is the whole
    mechanism, so this pins portfolio-last against a silent spread reorder.
    """
    root = Path(__file__).resolve().parents[2]
    contracts = {contract.id: contract for _, contract in load_insight_contracts(root)}
    scope = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")}))
    base = _row(1)
    colliding = CampaignPortfolioRow(
        SafeCampaignDisplaySignature(
            effective_name="Portfolio attributed ROAS",
            channel=base.signature.channel,
            marketplace=base.signature.marketplace,
            platform=base.signature.platform,
            effective_name_status=EffectiveNameStatus.AVAILABLE,
            effective_name_valid_from=base.signature.effective_name_valid_from,
        ),
        base.capability,
        (
            base.comparisons[0],
            # Diverging (portfolio moves +0.5) with a prior distinguishable from the
            # portfolio total's prior of 2, so a wrong-direction merge is detectable.
            _comparison(
                CampaignPortfolioMetric.ATTRIBUTED_ROAS,
                "1.5",
                "3",
                RATIO,
                CAMPAIGN_PORTFOLIO_ROW_GRAIN,
            ),
            *base.comparisons[2:],
        ),
        base.freshness_qualifiers,
    )
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(_row(0), colliding),
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )

    derivation, _ = portfolio_insight_request_derivation(
        artifact=artifact, scope=scope, contracts=contracts
    )

    divergence = derivation.evidence_by_slot[InsightSlotId.DIVERGENCE]
    colliding_percentages = [
        item
        for item in divergence.findings
        if item.kind is InsightFindingKind.PERCENTAGE_CHANGE
        and item.finding.name == "Portfolio attributed ROAS"
    ]
    assert colliding_percentages, "the collision fixture must admit divergence percentages"
    # Each basis carries its own priors (ADR-076 §5): the portfolio total keeps its prior
    # (2) and the same-named campaign keeps its own (3); neither attaches to the other.
    assert {(item.basis, item.prior_magnitude) for item in colliding_percentages} == {
        (OperandBasis.PORTFOLIO_WINDOW, Decimal("2")),
        (OperandBasis.CAMPAIGN_WINDOW, Decimal("3")),
    }


@pytest.mark.covers("ADR-068")
def test_insight_operands_stay_unique_when_campaigns_share_a_display_name() -> None:
    """Two channels can carry the same campaign name; findings must not collide on it."""
    _derivation(
        (
            _row_named(0, "Northstar Home | Brand", "Amazon DSP"),
            _row_named(1, "Northstar Home | Brand", "Sponsored Brands"),
        )
    )


@pytest.mark.covers("ADR-068")
def test_insight_operands_name_an_unnamed_campaign_deterministically() -> None:
    """A campaign without a display name still contributes a finding under its placeholder."""
    _derivation((_row_named(0, None, "Amazon DSP"), _row_named(1, "Campaign 001", "Amazon DSP")))


@pytest.mark.covers("ADR-068")
def test_insight_percentage_findings_skip_a_zero_prior_without_refusing_the_portfolio() -> None:
    """A campaign new this window has no rate of change; it keeps its absolute change only."""
    _derivation(
        (
            _row_named(0, "Campaign 000", "Amazon DSP", prior_spend="0"),
            _row_named(1, "Campaign 001", "Amazon DSP"),
        )
    )


@pytest.mark.covers("ADR-071")
class TestEstateReach:
    def _complete(self, count: int) -> CampaignPortfolioArtifact:
        return _build_served(
            period=PERIOD,
            sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
            rows=tuple(_row(index) for index in range(count)),
            totals=_totals(),
            trend=_trend(),
            evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
        )

    def test_a_sort_request_selects_a_complete_ordering_and_resets_position(self) -> None:
        artifact = self._complete(5)
        served = campaign_portfolio_segment(
            artifact, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "asc"), 0
        )
        assert served is not None
        assert served.sort.direction == "asc"
        values = tuple(
            Decimal(row.comparison_for(CampaignPortfolioMetric.SPEND).current.number)
            for row in served.rows
        )
        assert values == tuple(sorted(values))
        assert served.pagination is not None
        assert served.pagination.segment_index == 0
        # The name ordering is a declared comparator too.
        by_name = campaign_portfolio_segment(
            artifact, CampaignPortfolioSort(CampaignPortfolioSortField.NAME, "asc"), 0
        )
        assert by_name is not None
        keys = tuple(row.signature.sort_key for row in by_name.rows)
        assert keys == tuple(sorted(keys))

    def test_an_out_of_range_segment_is_refused_not_clamped(self) -> None:
        artifact = self._complete(5)
        assert (
            campaign_portfolio_segment(
                artifact, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"), 1
            )
            is None
        )

    def test_the_roster_covers_the_population_in_canonical_positions(self) -> None:
        artifact = self._complete(5)
        assert len(artifact.roster) == 5
        assert tuple(entry.position for entry in artifact.roster) == tuple(range(5))
        canonical = order_campaign_portfolio_rows(
            artifact.rows, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc")
        )
        assert tuple(entry.name for entry in artifact.roster) == tuple(
            row.signature.effective_name for row in canonical
        )
        payload = campaign_portfolio_artifact_payload(artifact)
        assert campaign_portfolio_artifact_from_payload(payload) == artifact


@pytest.mark.covers("ADR-071")
def test_every_declared_ordering_slices_the_full_estate_without_loss_or_duplication() -> None:
    rows = tuple(_row(index) for index in range(123))
    artifact = _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=rows,
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )
    expected_signatures = {row.signature for row in rows}

    for field in CampaignPortfolioSortField:
        for direction in ("asc", "desc"):
            sort = CampaignPortfolioSort(field, direction)
            expected = order_campaign_portfolio_rows(rows, sort)
            segments = tuple(
                campaign_portfolio_segment(artifact, sort, index) for index in range(3)
            )
            assert all(segment is not None for segment in segments)
            served = tuple(
                row for segment in segments if segment is not None for row in segment.rows
            )
            assert served == expected
            assert len({row.signature for row in served}) == len(rows)
            assert {row.signature for row in served} == expected_signatures
            assert tuple(len(segment.rows) for segment in segments if segment is not None) == (
                50,
                50,
                23,
            )

    tail = artifact.roster[-1]
    assert tail.position == 122
    canonical_tail = campaign_portfolio_segment(
        artifact,
        CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        tail.position // CAMPAIGN_PORTFOLIO_MAX_ROWS,
    )
    assert canonical_tail is not None
    assert any(row.signature.effective_name == tail.name for row in canonical_tail.rows)


@pytest.mark.covers("ADR-071")
def test_metric_ties_always_fall_back_to_the_safe_signature() -> None:
    def tied_row(index: int) -> CampaignPortfolioRow:
        base = _row(index)
        spend = _comparison(
            CampaignPortfolioMetric.SPEND,
            "10",
            "5",
            USD,
            CAMPAIGN_PORTFOLIO_ROW_GRAIN,
        )
        return CampaignPortfolioRow(
            base.signature,
            base.capability,
            (spend, *base.comparisons[1:]),
            base.freshness_qualifiers,
        )

    tied = tuple(tied_row(index) for index in range(12))
    for direction in ("asc", "desc"):
        ordered = order_campaign_portfolio_rows(
            tied, CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, direction)
        )
        assert tuple(row.signature.sort_key for row in ordered) == tuple(
            sorted(row.signature.sort_key for row in tied)
        )


@pytest.mark.covers("ADR-071")
def test_name_order_is_name_primary_across_mixed_dimensions() -> None:
    rows = (
        _row_named(0, "Zulu", "A channel"),
        _row_named(1, "Alpha", "Z channel"),
        _row_named(2, "Mike", "M channel"),
    )

    ascending = order_campaign_portfolio_rows(
        rows, CampaignPortfolioSort(CampaignPortfolioSortField.NAME, "asc")
    )
    descending = order_campaign_portfolio_rows(
        rows, CampaignPortfolioSort(CampaignPortfolioSortField.NAME, "desc")
    )

    assert tuple(row.signature.effective_name for row in ascending) == (
        "Alpha",
        "Mike",
        "Zulu",
    )
    assert tuple(row.signature.effective_name for row in descending) == (
        "Zulu",
        "Mike",
        "Alpha",
    )


def _display_portfolio() -> CampaignPortfolioArtifact:
    """Build a portfolio whose row spend is 75459.6400 USD (dollars, four places)."""
    row = _row(0)
    spend = _comparison(
        CampaignPortfolioMetric.SPEND, "75459.6400", "5", USD, CAMPAIGN_PORTFOLIO_ROW_GRAIN
    )
    return _build_served(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=(CampaignPortfolioRow(row.signature, row.capability, (spend, *row.comparisons[1:])),),
        totals=_totals(),
        trend=_trend(),
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
    )


def _row_values(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = cast("list[dict[str, object]]", payload["rows"])
    comparisons = cast("list[dict[str, object]]", rows[0]["comparisons"])
    totals = cast("list[dict[str, object]]", payload["totals"])
    return [
        cast("dict[str, object]", comparison[side])
        for comparison in (*comparisons, *totals)
        for side in ("current", "prior")
    ]


@pytest.mark.covers("ADR-084")
def test_portfolio_values_carry_a_two_place_display_value_for_currency_only() -> None:
    payload = campaign_portfolio_artifact_payload(_display_portfolio())

    values = _row_values(payload)
    assert values[0]["display_value"] == "75459.64"
    for value in values:
        units = cast("dict[str, object]", value["units"])
        assert (value["display_value"] is None) == (units["kind"] != "currency")


@pytest.mark.covers("ADR-084")
def test_a_retained_pre_display_portfolio_decodes_and_re_encodes_with_display_values() -> None:
    artifact = _display_portfolio()
    retained = cast(
        "dict[str, object]", without_display_values(campaign_portfolio_artifact_payload(artifact))
    )
    assert all("display_value" not in value for value in _row_values(retained))

    decoded = campaign_portfolio_artifact_from_payload(retained)

    assert decoded == artifact
    assert _row_values(campaign_portfolio_artifact_payload(decoded))[0]["display_value"] == (
        "75459.64"
    )


@pytest.mark.covers("ADR-084")
def test_the_portfolio_decoder_refuses_a_wrong_count_or_mixed_display_value() -> None:
    payload = campaign_portfolio_artifact_payload(_display_portfolio())
    wrong = json.loads(json.dumps(payload))
    _row_values(wrong)[0]["display_value"] = "75459.65"
    on_count = json.loads(json.dumps(payload))
    next(
        value
        for value in _row_values(on_count)
        if cast("dict[str, object]", value["units"])["kind"] == "count"
    )["display_value"] = "3.00"
    mixed = json.loads(json.dumps(payload))
    del _row_values(mixed)[0]["display_value"]

    for body in (wrong, on_count):
        with pytest.raises(ValueError, match="not canonical"):
            campaign_portfolio_artifact_from_payload(body)
    with pytest.raises(ValueError, match="mixes display values"):
        campaign_portfolio_artifact_from_payload(mixed)


# --- Typed-absent ratios over a zero denominator (ADR-087) ----------------------------------


def _undefined_roas_row(row: CampaignPortfolioRow) -> CampaignPortfolioRow:
    """Return the row with its current ROAS undefined (zero spend in the current window)."""
    comparisons = list(row.comparisons)
    index = next(
        position
        for position, item in enumerate(comparisons)
        if item.metric is CampaignPortfolioMetric.ATTRIBUTED_ROAS
    )
    roas = comparisons[index]
    comparisons[index] = CampaignPortfolioComparison(
        roas.metric,
        replace(roas.current, value=None),
        roas.prior,
        None,
        None,
    )
    return replace(row, comparisons=tuple(comparisons))


@pytest.mark.covers("ADR-087")
def test_only_a_quotient_value_may_be_typed_absent() -> None:
    assert _value("0", USD, PERIOD, CAMPAIGN_PORTFOLIO_ROW_GRAIN).number == 0
    absent = CampaignPortfolioMetricValue(
        None, RATIO, MeasurementBasis.AD_ATTRIBUTED, PERIOD, CAMPAIGN_PORTFOLIO_ROW_GRAIN
    )
    assert absent.value is None
    with pytest.raises(ValueError, match="typed absent"):
        _ = absent.number
    with pytest.raises(ValueError, match="only a quotient"):
        CampaignPortfolioMetricValue(
            None, COUNT, MeasurementBasis.AD_ATTRIBUTED, PERIOD, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        )
    # A cost per click over no clicks is a currency quotient; spend never is (ADR-087).
    spend_absent = CampaignPortfolioMetricValue(
        None, USD, MeasurementBasis.AD_ATTRIBUTED, PERIOD, CAMPAIGN_PORTFOLIO_ROW_GRAIN
    )
    prior = _value("2", USD, PRIOR, CAMPAIGN_PORTFOLIO_ROW_GRAIN)
    assert (
        CampaignPortfolioComparison(
            CampaignPortfolioMetric.CPC, spend_absent, prior, None, None
        ).change
        is None
    )
    with pytest.raises(ValueError, match="only a quotient"):
        CampaignPortfolioComparison(CampaignPortfolioMetric.SPEND, spend_absent, prior, None, None)


@pytest.mark.covers("ADR-087")
def test_an_undefined_ratio_comparison_carries_no_change() -> None:
    current = CampaignPortfolioMetricValue(
        None, RATIO, MeasurementBasis.AD_ATTRIBUTED, PERIOD, CAMPAIGN_PORTFOLIO_ROW_GRAIN
    )
    prior = _value("2", RATIO, PRIOR, CAMPAIGN_PORTFOLIO_ROW_GRAIN)
    metric = CampaignPortfolioMetric.ATTRIBUTED_ROAS
    assert CampaignPortfolioComparison(metric, current, prior, None, None).absolute_change is None
    for change, percent in ((Decimal("-2"), None), (None, "-100.00%"), (Decimal(0), "+0.00%")):
        with pytest.raises(ValueError, match="carries no change"):
            CampaignPortfolioComparison(metric, current, prior, change, percent)
    # Positive control: a defined pair still requires its exact delta.
    defined = _value("3", RATIO, PERIOD, CAMPAIGN_PORTFOLIO_ROW_GRAIN)
    with pytest.raises(ValueError, match="exact server delta"):
        CampaignPortfolioComparison(metric, defined, prior, None, "+50.00%")


@pytest.mark.covers("ADR-087")
def test_a_typed_absent_ratio_round_trips_and_a_null_additive_value_is_refused() -> None:
    rows = tuple(
        portfolio_row(
            campaign_display_signature(f"Campaign {index}"),
            CampaignSelectionCapability(f"{index:043d}", datetime(2099, 1, 1, tzinfo=UTC)),
            PERIOD,
            Decimal(index + 10),
        )
        for index in range(3)
    )
    artifact = complete_portfolio(PERIOD, (_undefined_roas_row(rows[0]), *rows[1:]))
    payload = json.loads(json.dumps(campaign_portfolio_artifact_payload(artifact)))
    served = next(
        row for row in payload["rows"] if row["comparisons"][1]["current"]["value"] is None
    )
    roas = served["comparisons"][1]
    assert (roas["absolute_change"], roas["percentage_change"]) == (None, None)
    assert roas["current"]["display_value"] is None
    assert campaign_portfolio_artifact_from_payload(payload) == artifact
    # A format-5 body carries no null: it still decodes byte-identically under this reader.
    defined = complete_portfolio(PERIOD, rows)
    old_body = json.loads(json.dumps(campaign_portfolio_artifact_payload(defined)))
    assert all(
        item["current"]["value"] is not None
        for row in old_body["rows"]
        for item in row["comparisons"]
    )
    assert campaign_portfolio_artifact_from_payload(old_body) == defined
    # A null on an additive metric is refused, as every null was before format 6.
    refused = json.loads(json.dumps(old_body))
    refused["rows"][0]["comparisons"][0]["current"]["value"] = None
    refused["rows"][0]["comparisons"][0]["current"]["display_value"] = None
    refused["rows"][0]["comparisons"][0]["absolute_change"] = None
    refused["rows"][0]["comparisons"][0]["percentage_change"] = None
    refused["rows"][0]["comparisons"][0]["change"] = None
    with pytest.raises(ValueError, match="only a quotient"):
        campaign_portfolio_artifact_from_payload(refused)


@pytest.mark.covers("ADR-087")
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_an_undefined_ratio_sorts_after_every_value(direction: str) -> None:
    rows = tuple(
        portfolio_row(
            campaign_display_signature(f"Campaign {index}"),
            CampaignSelectionCapability(f"{index:043d}", datetime(2099, 1, 1, tzinfo=UTC)),
            PERIOD,
            Decimal(index + 10),
        )
        for index in range(3)
    )
    undefined = _undefined_roas_row(rows[1])
    ordered = order_campaign_portfolio_rows(
        (rows[0], undefined, rows[2]),
        CampaignPortfolioSort(CampaignPortfolioSortField.ATTRIBUTED_ROAS, direction),
    )
    assert ordered[-1] is undefined
