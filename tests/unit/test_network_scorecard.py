# SPDX-License-Identifier: MIT-0
"""The per-network ROI Scorecard: artifact rules, insight contract and materializer (§6.1).

Every figure is fictional and synthetic. The materializer tests run the real measurement
batch, planner and shaping against an in-memory query port, as the Retail Health tests do.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from publication_support import StubPublications

from adapters.config import PresetArtifactBakeSettings
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher import scorecard_source
from adapters.jobs.preset_artifact_publisher.baker import (
    PAGE_BAKERS,
    BakeUnavailable,
    CampaignPresetArtifactBaker,
    RegisteredPresetArtifactBaker,
    UnavailableCampaignPresetArtifactSource,
    UnavailablePresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    SCORECARD_PAGE_ID,
    RetainedScorecardPresetArtifactSource,
    ScorecardArtifactRenderer,
    ScorecardLabels,
    ScorecardMeasurementBatch,
    ScorecardMeasurementBatchRequest,
    ScorecardRenderInput,
    ScorecardRenderResult,
    retained_scorecard_preset_artifact_source,
    scorecard_coverage_requirements,
    scorecard_labels,
    scorecard_measurement_batch,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.bake_sources import BakeSourceReads
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.telemetry.reporting import Report
from domain.catalog.freshness import Coverage, FreshnessStatus
from domain.measurement.planning import measure_alias
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.alert_rules import AlertDirection, AlertRule, AlertRuleScope
from domain.presentation.compared_metrics import scorecard_change_semantics
from domain.presentation.evidence_status import (
    CoverageFact,
    EvidenceStatus,
    write_evidence_status,
)
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    InsightHorizonRole,
    InsightSlotId,
    InsightSlotState,
)
from domain.presentation.page_schemas import (
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPublicationRequest,
    PresetArtifactVariant,
)
from domain.presentation.scorecard import (
    CHANNEL_TABLE_FOOT,
    DEDUPLICATED_MISSING_REASON,
    DEDUPLICATED_WITHHELD_REASON,
    RANKING_MISSING_TEXT,
    RANKING_WITHHELD_TEXT,
    SCORECARD_COMPARED_METRICS,
    CrossNetworkSpendRow,
    NetworkCard,
    NetworkKey,
    NetworkScorecardArtifact,
    NetworkScorecardSlotRequest,
    ScorecardBasis,
    ScorecardBasisKind,
    ScorecardChannelRow,
    ScorecardComparison,
    ScorecardFindingId,
    ScorecardHeadline,
    ScorecardModelValue,
    ScorecardOutcome,
    ScorecardRankingOutcome,
    cross_network_spend_rows,
    network_scorecard_artifact_payload,
    network_scorecard_slot,
    network_spend_total,
    ranking_outcome_text,
    scorecard_calendar_period,
    scorecard_ranking,
)
from domain.shared_kernel.provenance import MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
from ports.outbound.query import QueryRequest, QueryResult, QueryRow
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoveragePort,
    SourceCoverageRequest,
    SourceCoverageResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-106")]


def _reads() -> BakeSourceReads:
    """Return a fictional pinnable reader: composition builds over it and never reads."""
    return BakeSourceReads(cast("Any", None), cast("Any", None))


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
INSIGHTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
LABELS = scorecard_labels(ROOT, CATALOG)
SEMANTICS = scorecard_change_semantics(CATALOG)
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
PRIOR = DateRange(date(2026, 8, 6), date(2026, 8, 12))
MARKETPLACE = "amazon_us"
UK = "amazon_uk"
"""A fictional second governed marketplace; the tests add it to the labels' marketplaces."""
CURRENCIES = {MARKETPLACE: "USD", UK: "GBP"}
USD = Units(UnitKind.CURRENCY, "USD")
DSP = "amazon_dsp"
SP = "amazon_sponsored_products"
SB = "amazon_sponsored_brands"
SD = "amazon_sponsored_display"
"""Delivered only when a test asks for all four channels (the Scorecard's SC-1 state)."""

# Fictional (current, prior) operand values per channel. SP's ROAS moves 4.00 -> 4.50.
_VALUES: dict[str, dict[str, tuple[Decimal, Decimal]]] = {
    "ad_spend": {
        DSP: (Decimal(400), Decimal(400)),
        SP: (Decimal(600), Decimal(500)),
        SB: (Decimal(250), Decimal(240)),
        SD: (Decimal(150), Decimal(160)),
    },
    "attributed_revenue_14d": {
        DSP: (Decimal(1200), Decimal(1200)),
        SP: (Decimal(2700), Decimal(2000)),
        SB: (Decimal(1000), Decimal(900)),
        SD: (Decimal(450), Decimal(500)),
    },
    "attributed_revenue_incl_halo_14d": {
        DSP: (Decimal(1320), Decimal(1320)),
        SP: (Decimal(2970), Decimal(2200)),
        SB: (Decimal(1100), Decimal(990)),
        SD: (Decimal(495), Decimal(550)),
    },
}
# Deduplicated revenue per model and channel; SP leads under every model. Each model credits
# the same purchases once, so each model's total over any one set of channels is the same.
_DEDUPLICATED: dict[str, dict[str, Decimal]] = {
    "first_touch_v1": {DSP: Decimal(1500), SP: Decimal(2460), SB: Decimal(900), SD: Decimal(430)},
    "last_touch_v1": {DSP: Decimal(1300), SP: Decimal(2660), SB: Decimal(1000), SD: Decimal(330)},
    "linear_v1": {DSP: Decimal(1400), SP: Decimal(2560), SB: Decimal(950), SD: Decimal(380)},
}
_REVENUE_METRICS = frozenset(
    {
        "attributed_revenue_14d",
        "attributed_revenue_incl_halo_14d",
        "attributed_revenue_deduplicated_incl_halo_14d",
    }
)
_VENDOR_REVENUE = Decimal(20000)


@dataclass
class ScorecardQueries:
    """Deterministic grouped rows per governed plan; every figure is fictional."""

    period: DateRange
    channels: tuple[str, ...] = (DSP, SP)
    marketplaces: tuple[str, ...] = (MARKETPLACE,)
    """The workspace's marketplaces; an unpinned plan would read rows from all of them."""
    seen: list[QueryRequest] = field(default_factory=list[QueryRequest])
    revenue_scale: dict[str, Decimal] = field(default_factory=dict[str, Decimal])
    """Per marketplace, a factor on every attributed revenue, so two marketplaces differ."""

    def _value(  # noqa: PLR0913 — one figure's coordinates, by keyword
        self,
        metric: str,
        model: str | None,
        *,
        channel: str | None,
        current: bool,
        marketplace: str = MARKETPLACE,
    ) -> Decimal:
        channels = self.channels if channel is None else (channel,)
        scale = (
            self.revenue_scale.get(marketplace, Decimal(1))
            if metric in _REVENUE_METRICS
            else Decimal(1)
        )
        if metric == "attributed_revenue_deduplicated_incl_halo_14d":
            return scale * sum(
                (_DEDUPLICATED[cast("str", model)][item] for item in channels), Decimal(0)
            )
        if metric == "retail_revenue_shipped":
            return _VENDOR_REVENUE
        index = 0 if current else 1
        return scale * sum((_VALUES[metric][item][index] for item in channels), Decimal(0))

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        plan = request.plan.logical
        group = plan.group_by
        current = plan.window == self.period
        model = plan.filters.get("attribution_model_key")
        # A plan pinned to one marketplace reads only its rows, in that marketplace's currency.
        pinned = plan.filters.get("marketplace_key")
        per_channel = "channel_key" in group
        rows: list[QueryRow] = []
        for marketplace, channel in (
            (market, item)
            for market in (self.marketplaces if pinned is None else (pinned,))
            for item in (self.channels if per_channel else (None,))
        ):
            row: dict[str, str | int | Decimal | None] = {}
            for column in group:
                row[column] = {
                    "ecosystem_key": "amazon",
                    "platform_key": "amazon_ads",
                    "marketplace_key": marketplace,
                    "channel_key": channel,
                }[column]
            for column in plan.carried_columns:
                row[column] = CURRENCIES[marketplace]
            metric = plan.metric
            names = (
                (metric.numerator, metric.denominator) if plan.join is not None else (metric.name,)
            )
            for index, name in enumerate(names):
                row[measure_alias(index)] = self._value(
                    name, model, channel=channel, current=current, marketplace=marketplace
                )
            rows.append(row)
        return QueryResult(
            rows=tuple(rows), bytes_scanned=1_024, truncated=False, suppression=Suppression(False)
        )


def _renderer(
    queries: ScorecardQueries,
    publications: StubPublications | None = None,
    labels: ScorecardLabels = LABELS,
) -> ScorecardArtifactRenderer:
    stub = publications or StubPublications()
    return ScorecardArtifactRenderer(
        catalog=CATALOG,
        contracts=CONTRACTS,
        evidence=cast("PresentationEvidenceInspectionPort", stub),
        tooling=MeasurementTooling(queries=queries, publications=stub),
        labels=labels,
        insight_contract=INSIGHTS[InsightSlotId.NETWORK_SCORECARD],
        clock=lambda: NOW,
        semantics=SEMANTICS,
    )


def _batch(labels: ScorecardLabels, prior: DateRange | None = PRIOR) -> ScorecardMeasurementBatch:
    return scorecard_measurement_batch(
        ScorecardMeasurementBatchRequest(
            CATALOG,
            CONTRACTS,
            tuple(key for key, _ in labels.models),
            PERIOD,
            prior,
            NOW,
            labels.marketplaces,
        )
    )


def _rendered(
    prior: DateRange | None = PRIOR, publications: StubPublications | None = None
) -> NetworkScorecardArtifact:
    result = _renderer(ScorecardQueries(PERIOD), publications).render_scorecard(
        ScorecardRenderInput(PERIOD, prior)
    )
    assert result.artifact is not None
    assert result.publication_identities
    return result.artifact


# --- Insight contract --------------------------------------------------------------------


def test_network_scorecard_is_a_deterministic_observed_contract() -> None:
    contract = INSIGHTS[InsightSlotId.NETWORK_SCORECARD]
    assert InsightSlotId.NETWORK_SCORECARD in DETERMINISTIC_SLOT_IDS
    assert contract.output_class.value == "observed"
    assert contract.initial_fictional_prompt is None
    assert contract.deep is None
    assert [kind.value for kind in contract.deterministic_findings] == [
        "share-of-total",
        "percentage-change",
    ]
    kinds = {case.kind.value: case for case in contract.calibration_cases}
    # The two rejection cases the design names, and the known-correct DSP share.
    assert "Fictional Network B" in kinds["incompatible-basis"].findings[1].name
    assert "two different network totals" in kinds["miscomputed-derivation"].input_summary
    assert kinds["known-correct"].findings[0].name == "DSP share of Amazon network spend"


# --- Typed non-additive rule (domain layer) -------------------------------------------------


def _basis(kind: ScorecardBasisKind, network: str = "Amazon") -> ScorecardBasis:
    if kind is ScorecardBasisKind.DEDUPLICATED:
        return ScorecardBasis(kind, network, 14, "Linear")
    attributed = kind in {ScorecardBasisKind.ATTRIBUTED, ScorecardBasisKind.ATTRIBUTED_INCL_HALO}
    return ScorecardBasis(kind, network, 14 if attributed else None)


def _row(  # noqa: PLR0913 — units and network are keyword-only
    channel: str, spend: Decimal, *, units: Units = USD, network: str = "Amazon"
) -> ScorecardChannelRow:
    ratio = Units(UnitKind.RATIO)
    return ScorecardChannelRow(
        channel,
        channel,
        ScorecardComparison(
            "ad_spend", spend, spend, units, _basis(ScorecardBasisKind.SPEND, network)
        ),
        ScorecardComparison(
            "attributed_revenue_14d",
            spend * 3,
            None,
            units,
            _basis(ScorecardBasisKind.ATTRIBUTED, network),
        ),
        ScorecardComparison(
            "roas_attributed_14d",
            Decimal(3),
            None,
            ratio,
            _basis(ScorecardBasisKind.ATTRIBUTED, network),
        ),
        ScorecardComparison(
            "roas_attributed_incl_halo_14d",
            Decimal("3.3"),
            None,
            ratio,
            _basis(ScorecardBasisKind.ATTRIBUTED_INCL_HALO, network),
        ),
    )


def _card(  # noqa: PLR0913 — currency and label are keyword-only
    platform: str, marketplace: str, *, currency: str, label: str
) -> NetworkCard:
    units = Units(UnitKind.CURRENCY, currency)
    rows = (_row(SP, Decimal(100), units=units, network=label),)
    current, prior = network_spend_total(rows, currency)
    return NetworkCard(
        NetworkKey("amazon" if label == "Amazon" else "other", platform, marketplace),
        label,
        currency,
        ScorecardComparison(
            "ad_spend", current, prior, units, _basis(ScorecardBasisKind.SPEND, label)
        ),
        ScorecardHeadline(
            (ScorecardModelValue("linear_v1", "Linear", Decimal(3)),),
            "linear_v1",
            (_basis(ScorecardBasisKind.DEDUPLICATED, label),),
        ),
        scorecard_ranking(
            ((SP, SP, Decimal(100), {"linear_v1": Decimal(3)}),),
            (("linear_v1", "Linear"),),
            None,
        ),
        rows,
        (),
        "United States",
    )


def _status() -> EvidenceStatus:
    return write_evidence_status(
        PERIOD, (CoverageFact("spend:amazon", "Ad spend, all channels", "Network card"),)
    )


def test_basis_labels_name_network_attribution_basis_and_window() -> None:
    assert _basis(ScorecardBasisKind.ATTRIBUTED).text == (
        "Amazon-attributed · 14-day · conversion date · excludes brand halo"
    )
    assert _basis(ScorecardBasisKind.ATTRIBUTED_INCL_HALO).text == (
        "Amazon-attributed · 14-day · includes brand halo"
    )
    assert _basis(ScorecardBasisKind.DEDUPLICATED).text == (
        "Deduplicated across DSP and sponsored · Linear · 14-day · includes brand halo"
    )
    assert _basis(ScorecardBasisKind.MIXED).text == "Mixed basis: ad spend ÷ Vendor shipped revenue"
    # Refusal: an attributed figure can't be built without its window.
    with pytest.raises(ValueError, match="an attributed scorecard figure carries its window"):
        ScorecardBasis(ScorecardBasisKind.ATTRIBUTED, "Amazon")


def test_spend_totals_inside_one_currency_and_refuses_a_second() -> None:
    # Positive control: two channels in one currency add.
    assert network_spend_total((_row(DSP, Decimal(40)), _row(SP, Decimal(60))), "USD") == (
        Decimal(100),
        Decimal(100),
    )
    with pytest.raises(ValueError, match="a spend total never adds two currencies"):
        network_spend_total(
            (_row(DSP, Decimal(40)), _row(SP, Decimal(60), units=Units(UnitKind.CURRENCY, "EUR"))),
            "USD",
        )


def test_a_network_card_refuses_a_channel_figure_in_another_currency() -> None:
    euro = _row(SP, Decimal(100), units=Units(UnitKind.CURRENCY, "EUR"))
    with pytest.raises(ValueError, match="never converted across currencies"):
        NetworkCard(
            NetworkKey("amazon", "amazon_ads", MARKETPLACE),
            "Amazon",
            "USD",
            ScorecardComparison(
                "ad_spend", Decimal(100), Decimal(100), USD, _basis(ScorecardBasisKind.SPEND)
            ),
            _card("amazon_ads", MARKETPLACE, currency="USD", label="Amazon").headline,
            _card("amazon_ads", MARKETPLACE, currency="USD", label="Amazon").ranking,
            (euro,),
            (),
            "United States",
        )


def test_cross_network_spend_appears_only_with_two_networks_in_one_marketplace_and_currency() -> (
    None
):
    amazon = _card("amazon_ads", MARKETPLACE, currency="USD", label="Amazon")
    (single,) = cross_network_spend_rows((amazon,))
    assert single.spend is None
    assert single.text == "Networks loaded: Amazon"
    other = _card("other_ads", MARKETPLACE, currency="USD", label="Fictional Network B")
    (both,) = cross_network_spend_rows((amazon, other))
    assert both.spend == Decimal(200)
    assert both.text is None
    # Two marketplaces in one currency give two rows, never one.
    uk = _card("other_ads", "amazon_uk", currency="USD", label="Fictional Network B")
    assert [row.marketplace_key for row in cross_network_spend_rows((amazon, uk))] == [
        "amazon_uk",
        MARKETPLACE,
    ]
    # One marketplace with networks in two currencies gives two rows, never converted.
    euro = _card("other_ads", MARKETPLACE, currency="EUR", label="Fictional Network B")
    assert {row.currency_code for row in cross_network_spend_rows((amazon, euro))} == {
        "USD",
        "EUR",
    }
    assert all(row.spend is None for row in cross_network_spend_rows((amazon, euro)))


def test_the_artifact_carries_no_attributed_figure_above_one_channel_of_one_network() -> None:
    amazon = _card("amazon_ads", MARKETPLACE, currency="USD", label="Amazon")
    other = _card("other_ads", MARKETPLACE, currency="USD", label="Fictional Network B")
    artifact = NetworkScorecardArtifact(
        ScorecardOutcome.COMPLETE,
        PERIOD,
        PRIOR,
        (amazon, other),
        (),
        cross_network_spend_rows((amazon, other)),
        (),
        (network_scorecard_slot(NetworkScorecardSlotRequest((), "rev", "quiet")),),
        "7 days",
        evidence_status=_status(),
    )
    payload = network_scorecard_artifact_payload(artifact)
    attributed = {"attributed_revenue_14d", "roas_attributed_14d", "roas_attributed_incl_halo_14d"}

    def metrics(value: object, path: str) -> list[str]:
        if isinstance(value, dict):
            mapping = cast("dict[str, object]", value)
            found = [path] if mapping.get("metric") in attributed else []
            return found + [p for k, v in mapping.items() for p in metrics(v, f"{path}.{k}")]
        if isinstance(value, list):
            items = cast("list[object]", value)
            return [p for i, v in enumerate(items) for p in metrics(v, f"{path}[{i}]")]
        return []

    # Every attributed figure sits on one channel row of one card.
    assert metrics(payload, "$")
    assert all(".channel_table.rows[" in path for path in metrics(payload, "$"))
    # Paired: an attributed total planted on a card is found outside every channel row.
    planted = json.loads(json.dumps(payload))
    planted["cards"][0]["attributed_total"] = {"metric": "attributed_revenue_14d"}
    assert not all(".channel_table.rows[" in path for path in metrics(planted, "$"))
    (spend_row,) = payload["cross_network_spend"]  # type: ignore[misc]
    assert set(cast("dict[str, object]", spend_row)) == {
        "row_id",
        "label",
        "marketplace_text",
        "currency_code",
        "networks_loaded",
        "figure",
        "text",
        "reason",
    }
    # Refusal: cross-network rows can't be supplied apart from the cards they derive from.
    with pytest.raises(ValueError, match="derived from the cards alone"):
        NetworkScorecardArtifact(
            ScorecardOutcome.COMPLETE,
            PERIOD,
            PRIOR,
            (amazon, other),
            (),
            cross_network_spend_rows((amazon,)),
            (),
            artifact.insights,
            "7 days",
            evidence_status=_status(),
        )


def test_the_ranking_is_insufficient_rather_than_concluded_from_a_partial_set() -> None:
    models = (("first_touch_v1", "First touch"), ("linear_v1", "Linear"))
    dsp, sp = Decimal(40), Decimal(60)
    robust = scorecard_ranking(
        (
            (DSP, "Amazon DSP", dsp, {"first_touch_v1": Decimal(3), "linear_v1": Decimal(3)}),
            (SP, "Sponsored Products", sp, {"first_touch_v1": Decimal(4), "linear_v1": Decimal(5)}),
        ),
        models,
        None,
    )
    assert robust.outcome is ScorecardRankingOutcome.ROBUST_LEADER
    assert robust.agreed_leader_key == SP
    # Each row carries its channel's spend, as the LLD's ranking table lists it.
    assert [row.spend for row in robust.rows] == [dsp, sp]
    sensitive = scorecard_ranking(
        (
            (DSP, "Amazon DSP", dsp, {"first_touch_v1": Decimal(5), "linear_v1": Decimal(3)}),
            (SP, "Sponsored Products", sp, {"first_touch_v1": Decimal(4), "linear_v1": Decimal(5)}),
        ),
        models,
        None,
    )
    assert sensitive.outcome is ScorecardRankingOutcome.MODEL_SENSITIVE
    partial = scorecard_ranking(
        ((SP, "Sponsored Products", sp, {"first_touch_v1": Decimal(4), "linear_v1": None}),),
        models,
        None,
    )
    assert partial.outcome is ScorecardRankingOutcome.INSUFFICIENT
    assert [row.spend for row in partial.rows] == [sp]


def test_a_channel_a_model_credits_nothing_keeps_its_zero_and_takes_no_rank() -> None:
    """Owner decision 2026-10-22: a spending channel at 0 credit isn't a tied last place."""
    models = (("first_touch_v1", "First touch"), ("linear_v1", "Linear"))
    zero, one, two, four, five = (Decimal(n) for n in (0, 1, 2, 4, 5))
    ranking = scorecard_ranking(
        (
            (DSP, "Amazon DSP", Decimal(40), {"first_touch_v1": zero, "linear_v1": two}),
            (SP, "Sponsored Products", Decimal(60), {"first_touch_v1": five, "linear_v1": four}),
            (SB, "Sponsored Brands", Decimal(20), {"first_touch_v1": zero, "linear_v1": one}),
        ),
        models,
        None,
    )
    cells = {
        row.channel_key: [(item.value, item.rank) for item in row.values] for row in ranking.rows
    }
    # The zeros keep their governed value and take no rank; the credited channels rank densely.
    assert cells == {
        DSP: [(zero, None), (two, 2)],
        SP: [(five, 1), (four, 1)],
        SB: [(zero, None), (one, 3)],
    }
    assert ranking.outcome is ScorecardRankingOutcome.ROBUST_LEADER
    assert ranking.agreed_leader_key == SP
    # A stated ranking can't rank a zero, nor leave a credited value unranked.
    sb_row = ranking.rows[2]
    tied = replace(sb_row, values=(replace(sb_row.values[0], rank=4), sb_row.values[1]))
    with pytest.raises(ValueError, match="ranks every credited value and no zero"):
        replace(ranking, rows=(*ranking.rows[:2], tied))


# --- Materializer ------------------------------------------------------------------------


def test_the_batch_requests_every_attributed_figure_inside_one_network_and_channel() -> None:
    batch = _batch(LABELS)
    # The governed bridge declares one marketplace today.
    assert LABELS.marketplaces == (MARKETPLACE,)
    # 4 channel figures x 2 windows, 3 models x (network x 2 windows + channel), TACoS x 2.
    assert len(batch.prepared.plans) == 8 + 9 + 2
    for plan in batch.prepared.plans:
        logical = plan.logical
        # Every plan is pinned to one marketplace, so no answer spans two currencies.
        assert logical.filters["marketplace_key"] == MARKETPLACE
        if logical.metric.name == "tacos":
            continue
        assert {"ecosystem_key", "platform_key", "marketplace_key"} <= set(logical.group_by)
        if logical.metric.name != "roas_deduplicated_incl_halo_14d":
            assert "channel_key" in logical.group_by


def test_the_materializer_bakes_one_amazon_card_with_basis_labels_and_findings() -> None:
    artifact = _rendered()
    assert artifact.outcome is ScorecardOutcome.COMPLETE
    (card,) = artifact.cards
    assert card.network == NetworkKey("amazon", "amazon_ads", MARKETPLACE)
    assert card.network_label == "Amazon"
    assert card.currency_code == "USD"
    assert (card.network_spend.current, card.network_spend.prior) == (Decimal(1000), Decimal(900))
    rows = {row.channel_key: row for row in card.channel_rows}
    assert set(rows) == {DSP, SP}
    assert rows[SP].roas.current == Decimal("4.5")
    assert rows[SP].roas.prior == Decimal(4)
    assert rows[SP].roas.basis.text == (
        "Amazon-attributed · 14-day · conversion date · excludes brand halo"
    )
    assert rows[SP].roas_incl_halo.basis.text == "Amazon-attributed · 14-day · includes brand halo"
    # Absent governed Amazon channels are named, never shown as zero rows.
    absent = {item.channel_key for item in card.absent_channels}
    assert absent == {
        "amazon_sponsored_brands",
        "amazon_sponsored_display",
        "amazon_sponsored_television",
    }
    assert card.dsp_absence_text is None
    # Headline: one network only, every model, the first registered model as default.
    assert card.headline.default_model_key == "first_touch_v1"
    assert [item.value for item in card.headline.values] == [Decimal("3.96")] * 3
    assert card.ranking.outcome is ScorecardRankingOutcome.ROBUST_LEADER
    assert card.ranking.agreed_leader_key == SP
    (spend_row,) = artifact.cross_network_spend
    assert spend_row.text == "Networks loaded: Amazon"
    (tacos,) = artifact.tacos_rows
    assert tacos.tacos.current == Decimal("0.05")
    assert tacos.tacos.basis.text == "Mixed basis: ad spend ÷ Vendor shipped revenue"
    # Findings: each channel's share of Amazon spend, and SP's +12.50% ROAS move.
    by_id = {(item.finding_id, item.channel_key): item for item in artifact.findings}
    assert by_id[(ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND, DSP)].value == Decimal("40.00")
    mover = by_id[(ScorecardFindingId.CHANNEL_ROAS_MOVER, SP)]
    assert (mover.value, mover.material) == (Decimal("12.50"), True)
    assert by_id[(ScorecardFindingId.CHANNEL_ROAS_MOVER, DSP)].material is False
    (slot,) = artifact.insights
    assert slot.state is InsightSlotState.FILLED
    assert slot.text is not None
    assert slot.text.startswith(
        "Sponsored Products attributed ROAS rose 12.50% against the prior window, inside the "
        "Amazon network in amazon_us."
    )
    payload = network_scorecard_artifact_payload(artifact)
    assert json.dumps(payload)  # JSON-ready
    assert payload["cards"][0]["channel_table"]["foot"] == CHANNEL_TABLE_FOOT  # type: ignore[index]


def test_a_missing_deduplicated_delivery_withholds_the_headline_but_channel_rows_render() -> None:
    queries = ScorecardQueries(PERIOD)
    batch = _batch(LABELS)
    deduplicated = frozenset(
        index
        for index, plan in enumerate(batch.prepared.plans)
        if plan.logical.metric.name == "roas_deduplicated_incl_halo_14d"
    )
    publications = StubPublications(inspection_absent_indices=deduplicated)
    result = _renderer(queries, publications).render_scorecard(ScorecardRenderInput(PERIOD, PRIOR))
    assert result.artifact is not None
    (card,) = result.artifact.cards
    assert card.headline.absence_reason == DEDUPLICATED_MISSING_REASON
    assert all(item.value is None for item in card.headline.values)
    assert card.ranking.outcome is ScorecardRankingOutcome.INSUFFICIENT
    assert len(card.channel_rows) == 2


def test_missing_channel_delivery_bakes_nothing() -> None:
    publications = StubPublications(inspection_absent_indices=frozenset({0}))
    result = _renderer(ScorecardQueries(PERIOD), publications).render_scorecard(
        ScorecardRenderInput(PERIOD, PRIOR)
    )
    assert result.artifact is None


class _RecordingQueries:
    """A required `queries` port that records every request; an unconfigured source sends none."""

    def __init__(self) -> None:
        self.seen: list[QueryRequest] = []

    def run(self, request: QueryRequest) -> QueryResult:
        self.seen.append(request)
        raise AssertionError("an unconfigured source must never query")


def test_the_materializer_composes_on_its_page_entry_and_fails_closed_without_it(
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
    # The committed v2 `scorecard` entry (UI slice 9a) composes the source on its policy. The
    # clients are built, never called, so a fictional region is enough.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    queries = _RecordingQueries()
    pointers = cast("object", None)
    composed = retained_scorecard_preset_artifact_source(
        settings, cast("Any", pointers), cast("Any", queries), _reads()
    )
    assert composed is not None
    assert TemporalPresetId.LAST_COMPLETE_MONTH in composed.temporal_policy.candidate_presets
    # Paired: without the page entry the source doesn't compose.
    schemas = scorecard_source.load_v2_page_schemas(ROOT)
    without = replace(
        schemas, pages=tuple(page for page in schemas.pages if page.page_id != SCORECARD_PAGE_ID)
    )

    def _without(_root: pathlib.Path) -> object:
        return without

    monkeypatch.setattr(scorecard_source, "load_v2_page_schemas", _without)
    assert (
        retained_scorecard_preset_artifact_source(
            settings, cast("Any", pointers), cast("Any", queries), _reads()
        )
        is None
    )
    assert queries.seen == []
    # A5.3: scorecard has roster rows, so it has exactly one page->baker entry.
    assert SCORECARD_PAGE_ID in PAGE_BAKERS
    baker = RegisteredPresetArtifactBaker(
        bakers={
            "campaigns": CampaignPresetArtifactBaker(UnavailableCampaignPresetArtifactSource())
        },
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=("campaigns",),
    )
    result = baker.bake(BakeIntent(SCORECARD_PAGE_ID, "7d"))
    assert isinstance(result, BakeUnavailable)
    assert result.reason == "page_baker_unavailable"


def test_coverage_gates_on_spend_and_attributed_conversions_only() -> None:
    sources = {
        source
        for requirement in scorecard_coverage_requirements(CATALOG)
        for source in requirement.sources
    }
    assert sources
    assert all("vendor" not in str(source).lower() for source in sources)


def test_a_window_without_a_comparison_bakes_shares_and_no_roas_mover() -> None:
    """A calendar period shows no comparison, so `channel-roas-mover` stays silent (§6.1)."""
    artifact = _rendered(prior=None)
    assert artifact.prior_period is None
    kinds = {item.finding_id for item in artifact.findings}
    assert kinds == {ScorecardFindingId.CHANNEL_SHARE_OF_NETWORK_SPEND}
    (card,) = artifact.cards
    assert all(row.roas.prior is None for row in card.channel_rows)
    (slot,) = artifact.insights
    assert slot.state is InsightSlotState.FILLED
    assert slot.text is not None
    assert "Sponsored Products holds 60.00% of ad spend inside the Amazon network" in slot.text


# --- One answer per marketplace, a checked TACoS prior, reported invariant breaks -----------


def test_two_marketplaces_in_two_currencies_bake_one_card_each_never_converted() -> None:
    """Design §6.1: currency differs across marketplaces -> one card per marketplace."""
    labels = replace(LABELS, marketplaces=(UK, MARKETPLACE))
    queries = ScorecardQueries(PERIOD, marketplaces=(UK, MARKETPLACE))
    result = _renderer(queries, labels=labels).render_scorecard(ScorecardRenderInput(PERIOD, PRIOR))
    assert result.artifact is not None
    cards = {card.network.marketplace_key: card for card in result.artifact.cards}
    assert set(cards) == {UK, MARKETPLACE}
    assert (cards[UK].currency_code, cards[MARKETPLACE].currency_code) == ("GBP", "USD")
    assert all(row.spend.units == Units(UnitKind.CURRENCY, "GBP") for row in cards[UK].channel_rows)
    # Each card totals only its own marketplace: the same fictional figures, never added.
    assert cards[UK].network_spend.current == cards[MARKETPLACE].network_spend.current == 1000
    spend_rows = {
        (row.marketplace_key, row.currency_code) for row in result.artifact.cross_network_spend
    }
    assert spend_rows == {(UK, "GBP"), (MARKETPLACE, "USD")}
    assert all(row.spend is None for row in result.artifact.cross_network_spend)
    assert {row.marketplace_key for row in result.artifact.tacos_rows} == {UK, MARKETPLACE}
    # 19 plans per marketplace, every query pinned to one of them.
    assert len(_batch(labels).prepared.plans) == 38
    assert {request.plan.logical.filters["marketplace_key"] for request in queries.seen} == {
        UK,
        MARKETPLACE,
    }


def test_a_partial_marketplace_bakes_no_scorecard_rather_than_one_missing_it() -> None:
    labels = replace(LABELS, marketplaces=(UK, MARKETPLACE))
    batch = _batch(labels)
    # The UK channel spend answer has no publication: the whole bake fails closed.
    (uk_spend,) = (
        index
        for index, (label, plan) in enumerate(zip(batch.slots, batch.prepared.plans, strict=True))
        if label[:3] == (UK, "channel", "ad_spend") and plan.logical.window == PERIOD
    )
    publications = StubPublications(inspection_absent_indices=frozenset({uk_spend}))
    result = _renderer(
        ScorecardQueries(PERIOD, marketplaces=(UK, MARKETPLACE)), publications, labels
    ).render_scorecard(ScorecardRenderInput(PERIOD, PRIOR))
    assert result.artifact is None
    # Positive control: the same two-marketplace bake with every answer present.
    assert (
        _renderer(ScorecardQueries(PERIOD, marketplaces=(UK, MARKETPLACE)), labels=labels)
        .render_scorecard(ScorecardRenderInput(PERIOD, PRIOR))
        .artifact
        is not None
    )


def test_a_tacos_prior_over_a_partial_window_is_not_compared() -> None:
    batch = _batch(LABELS)
    (prior_tacos,) = (
        index
        for index, plan in enumerate(batch.prepared.plans)
        if plan.logical.metric.name == "tacos" and plan.logical.window == PRIOR
    )
    covered = DateRange(PRIOR.start + timedelta(days=2), PRIOR.end)
    hole = DateRange(PRIOR.start, PRIOR.start + timedelta(days=1))
    coverage = Coverage(
        fact_name="fct_ad_delivery + fct_retail_sales",
        window=covered,
        freshness=FreshnessStatus.FRESH,
        measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
        valid_grains=("marketplace",),
        loaded_at=NOW - timedelta(hours=2),
        intervals=(covered,),
        holes=(hole,),
    )
    publications = StubPublications(inspection_coverage_by_index={prior_tacos: coverage})
    (tacos,) = _rendered(publications=publications).tacos_rows
    # The current TACoS still renders; its prior is absent rather than a partial-window ratio.
    assert tacos.tacos.current == Decimal("0.05")
    assert tacos.tacos.prior is None
    # Positive control: a complete prior window is compared.
    (complete,) = _rendered().tacos_rows
    assert complete.tacos.prior is not None


def test_an_artifact_invariant_break_is_reported_and_bakes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[tuple[BaseException, Report]] = []

    def record(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    def underived(_cards: tuple[NetworkCard, ...]) -> tuple[CrossNetworkSpendRow, ...]:
        return ()

    monkeypatch.setattr(scorecard_source, "report_error", record)
    # Positive control: an ordinary bake reports nothing.
    assert _rendered() is not None
    assert reported == []
    # Cross-network rows that weren't derived from the cards break the artifact's invariant.
    monkeypatch.setattr(scorecard_source, "cross_network_spend_rows", underived)
    result = _renderer(ScorecardQueries(PERIOD)).render_scorecard(
        ScorecardRenderInput(PERIOD, PRIOR)
    )
    assert result.artifact is None
    ((error, report),) = reported
    assert isinstance(error, ValueError)
    assert "derived from the cards alone" in str(error)
    assert (report.operation, report.correlation_id) == (
        "scorecard_artifact_render",
        "scorecard-artifact",
    )


def test_a_ranking_row_carries_its_own_channel_spend() -> None:
    artifact = _rendered()
    (card,) = artifact.cards
    spend = {row.channel_key: row.spend.current for row in card.channel_rows}
    assert {row.channel_key: row.spend for row in card.ranking.rows} == spend
    payload = network_scorecard_artifact_payload(artifact)
    ranking_rows = payload["cards"][0]["ranking"]["rows"]  # type: ignore[index]
    assert {row["channel"]: row["spend_display_value"] for row in ranking_rows} == {  # type: ignore[index]
        key: f"{value:.2f}" for key, value in spend.items() if value is not None
    }
    # Refusal: a ranking row whose spend differs from its channel row is refused.
    wrong = replace(
        card.ranking,
        rows=tuple(replace(row, spend=row.spend + 1) for row in card.ranking.rows),
    )
    with pytest.raises(ValueError, match="a ranking row carries its own channel row's spend"):
        replace(card, ranking=wrong)


# --- UI slice 9a: the browser-shaped body (A2) ---------------------------------------------

_FORBIDDEN_KEYS = frozenset(
    {"amazon_asin", "campaign_key", "channel_key", "marketplace_key", "platform_key"}
)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        return set(mapping) | {key for item in mapping.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in cast("list[object]", value) for key in _keys(item)}
    return set()


def test_the_body_names_no_canonical_selector_the_browser_refuses() -> None:
    payload = network_scorecard_artifact_payload(_rendered())
    assert not _keys(payload) & _FORBIDDEN_KEYS
    # Paired: the same check finds a selector key planted on a channel row.
    planted = json.loads(json.dumps(payload))
    planted["cards"][0]["channel_table"]["rows"][0]["channel_key"] = SP
    assert _keys(planted) & _FORBIDDEN_KEYS == {"channel_key"}


def test_every_compared_figure_carries_its_server_change_and_display_value() -> None:
    payload = network_scorecard_artifact_payload(_rendered())
    card = cast("list[dict[str, object]]", payload["cards"])[0]
    spend = cast("dict[str, object]", card["spend"])
    assert spend["display_value"] == "1000.00"
    assert spend["prior_display_value"] == "900.00"
    assert spend["prior_label"] == "Aug 6\u201312"
    assert spend["basis"] == "Amazon Ads · spend across DSP and sponsored"
    assert cast("dict[str, object]", spend["change"])["sentiment"] == "neutral"
    rows = cast("dict[str, list[dict[str, object]]]", card["channel_table"])["rows"]
    sp = next(row for row in rows if row["channel"] == SP)
    figures = {
        figure["metric"]: figure for figure in cast("list[dict[str, object]]", sp["figures"])
    }
    roas_change = cast("dict[str, object]", figures["roas_attributed_14d"]["change"])
    assert (roas_change["direction"], roas_change["sentiment"]) == ("up", "favourable")
    assert roas_change["display_text"] == "+0.50\u00d7"
    # Attributed revenue and ROAS with halo show no chip (mockup), though their prior exists.
    assert figures["attributed_revenue_14d"]["change"] is None
    assert figures["roas_attributed_incl_halo_14d"]["change"] is None
    # A multiplier is served at two places, beside its exact value (plan S26, G9A-4).
    roas = figures["roas_attributed_14d"]
    assert (roas["value"], roas["display_value"]) == ("4.5000", "4.50")
    assert figures["roas_attributed_14d"]["format"] == "multiplier"
    # The card names the mockup's short marketplace, in its header and caption (D13c, G9A-5).
    assert card["title"] == "Amazon \u00b7 US marketplace \u00b7 USD"
    assert cast("dict[str, object]", card["channel_table"])["caption_detail"] == (
        "Amazon \u00b7 US \u00b7 Aug 13\u201319, 2026"
    )
    # TACoS is a rate: the shared percent formatter shows it, so it carries no display value.
    (tacos,) = cast("list[dict[str, Any]]", payload["tacos"])
    assert (tacos["figure"]["format"], tacos["figure"]["display_value"]) == ("percent", None)
    # One marketplace: the retail rows name none, as the approved mockup draws them.
    assert tacos["marketplace_text"] is None
    headline = cast("dict[str, object]", card["headline"])
    models = cast("list[dict[str, object]]", headline["models"])
    assert [model["option_label"] for model in models] == [
        "First touch (default)",
        "Last touch",
        "Linear",
    ]
    figure = cast("dict[str, object]", models[0]["figure"])
    # The headline compares with its own measured prior: 3960/1000 against 3960/900.
    assert figure["prior_value"] is not None
    assert cast("dict[str, object]", figure["change"])["direction"] == "down"
    assert payload["scope_text"] == (
        "Showing Aug 13\u201319, 2026 (7 days) compared with Aug 6\u201312. "
        "Data through Aug 19, 2026."
    )
    assert payload["evidence_status"] is not None
    assert cast("dict[str, object]", card["ranking"])["outcome_text"] == (
        "Sponsored Products leads under all three models."
    )
    assert cast("dict[str, object]", card["channel_table"])["absent_lines"] == [
        "No Sponsored Brands delivery in this window.",
        "No Sponsored Display delivery in this window.",
        "No Sponsored Television delivery in this window.",
    ]


def test_a_calendar_period_names_itself_and_compares_nothing() -> None:
    result = _renderer(ScorecardQueries(PERIOD)).render_scorecard(
        ScorecardRenderInput(PERIOD, None, "Latest measured month", "August 2026")
    )
    assert result.artifact is not None
    payload = network_scorecard_artifact_payload(result.artifact)
    assert payload["scope_text"] == (
        "Showing August 2026 (Latest measured month). No comparison for calendar periods."
    )
    assert payload["period_label"] == "August 2026"
    card = cast("list[dict[str, object]]", payload["cards"])[0]
    assert cast("dict[str, object]", card["spend"])["change"] is None
    # Paired: a calendar period with a prior window is refused by the artifact.
    with pytest.raises(ValueError, match="calendar period on the Scorecard compares nothing"):
        replace(result.artifact, prior_period=PRIOR)


def test_a_withheld_headline_reads_its_own_ranking_text_and_a_missing_one_reads_another() -> None:
    batch = _batch(LABELS)
    deduplicated = frozenset(
        index
        for index, plan in enumerate(batch.prepared.plans)
        if plan.logical.metric.name == "roas_deduplicated_incl_halo_14d"
    )
    missing = _renderer(
        ScorecardQueries(PERIOD), StubPublications(inspection_absent_indices=deduplicated)
    ).render_scorecard(ScorecardRenderInput(PERIOD, PRIOR))
    assert missing.artifact is not None
    (card,) = missing.artifact.cards
    assert ranking_outcome_text(card) == RANKING_MISSING_TEXT
    withheld = replace(
        card, ranking=replace(card.ranking, absence_reason=DEDUPLICATED_WITHHELD_REASON)
    )
    assert ranking_outcome_text(withheld) == RANKING_WITHHELD_TEXT
    status = missing.artifact.evidence_status
    assert status is not None
    headline = next(entry for entry in status.entries if entry.slot.startswith("headline:"))
    assert headline.state.value == "unavailable"


def test_a_spending_channel_with_no_deduplicated_ratio_names_the_deduplicated_hole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC9A-GATE-005: the ranking ranks the deduplicated figure, so its hole is named.

    `GroupedAnswer.rows` may carry a served null, here for SP, which spent. The channel
    table's attributed figures for SP still render (they come from other plans), so "this
    channel's figure hasn't arrived" would misstate the table, and "no ad spend" would
    contradict the spend it shows.
    """
    parse = scorecard_source.parse_grouped_answer

    def with_null(
        answer: Any, period: DateRange, grains: tuple[str, ...]
    ) -> scorecard_source.GroupedAnswer | None:
        parsed = parse(answer, period, grains)
        plan = answer.logical
        if (
            parsed is None
            or plan.metric.name != "roas_deduplicated_incl_halo_14d"
            or "channel_key" not in plan.group_by
            or plan.window != PERIOD
        ):
            return parsed
        rows = {key: None if SP in key else value for key, value in parsed.rows.items()}
        return replace(parsed, rows=rows)

    monkeypatch.setattr(scorecard_source, "parse_grouped_answer", with_null)
    result = _renderer(ScorecardQueries(PERIOD)).render_scorecard(
        ScorecardRenderInput(PERIOD, PRIOR)
    )
    monkeypatch.undo()
    assert result.artifact is not None
    (card,) = result.artifact.cards
    sp = next(row for row in card.ranking.rows if row.channel_key == SP)
    assert sp.spend == Decimal(600)
    assert {item.value for item in sp.values} == {None}
    assert card.ranking.outcome is ScorecardRankingOutcome.INSUFFICIENT
    assert card.ranking.absence_reason == DEDUPLICATED_MISSING_REASON
    assert ranking_outcome_text(card) == RANKING_MISSING_TEXT
    # The channel table's SP row still states its attributed ROAS.
    table = next(row for row in card.channel_rows if row.channel_key == SP)
    assert table.roas.current is not None
    # Paired: the same bake with the ratio served ranks SP first under every model.
    (ranked,) = _rendered().cards
    assert ranked.ranking.absence_reason is None
    assert ranked.ranking.outcome is ScorecardRankingOutcome.ROBUST_LEADER


@dataclass
class _DisagreeingHeadlineQueries(ScorecardQueries):
    """Last touch's complete network-grain answer credits 500 more than the other models.

    Every model credits each deduplicated purchase once, so the real AMC pack can't produce
    this; malformed input (the 2026-10-22 rotating dev feed) can.
    """

    def _value(  # noqa: PLR0913 — the parent's signature
        self,
        metric: str,
        model: str | None,
        *,
        channel: str | None,
        current: bool,
        marketplace: str = MARKETPLACE,
    ) -> Decimal:
        value = super()._value(
            metric, model, channel=channel, current=current, marketplace=marketplace
        )
        if (
            metric == "attributed_revenue_deduplicated_incl_halo_14d"
            and channel is None
            and model == "last_touch_v1"
        ):
            return value + Decimal(500)
        return value


MODELS_DISAGREE_REASON = (
    "The attribution models credited different deduplicated totals for this window."
)
MODELS_DISAGREE_TEXT = (
    "Not available: the attribution models credited different deduplicated totals for this "
    "window, so channels can't be ranked under each model."
)


def test_a_headline_that_differs_by_model_never_bakes_a_robust_leader() -> None:
    """Report §5.4: the bake checks the invariant its own model note asserts."""
    result = _renderer(_DisagreeingHeadlineQueries(PERIOD)).render_scorecard(
        ScorecardRenderInput(PERIOD, PRIOR)
    )
    assert result.artifact is not None
    (card,) = result.artifact.cards
    assert len({item.value for item in card.headline.values}) == 2
    assert card.ranking.outcome is ScorecardRankingOutcome.INSUFFICIENT
    assert card.ranking.agreed_leader_key is None
    assert card.ranking.absence_reason == MODELS_DISAGREE_REASON
    assert ranking_outcome_text(card) == MODELS_DISAGREE_TEXT
    assert {item.rank for row in card.ranking.rows for item in row.values} == {None}
    # Paired: the same bake with equal model totals names Sponsored Products the leader.
    (agreed,) = _rendered().cards
    assert len({item.value for item in agreed.headline.values}) == 1
    assert agreed.ranking.outcome is ScorecardRankingOutcome.ROBUST_LEADER


_SCORECARD_BAKED_SEMANTICS: tuple[tuple[int, str], ...] = (
    # (Scorecard render format, Scorecard compared-roster digest), oldest first; append only.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (2, "b8edba16df38e92d5999f775e49e78264c85ef24be3659d30613bd409c78ed5e"),
    # Slice 9a round 2: served copy fields, channel absence reasons, the TACoS coverage slot.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (3, "b8edba16df38e92d5999f775e49e78264c85ef24be3659d30613bd409c78ed5e"),
    # Slice 9a round 3: two-place multipliers, the short marketplace name, retail row identity.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (4, "b8edba16df38e92d5999f775e49e78264c85ef24be3659d30613bd409c78ed5e"),
    # Slice 9a round 4: insight cards and Also changed rows name their marketplace.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (5, "b8edba16df38e92d5999f775e49e78264c85ef24be3659d30613bd409c78ed5e"),
    # Slice 9a round 5: channel rows in the governed taxonomy's order, as the mockups draw them.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (6, "b8edba16df38e92d5999f775e49e78264c85ef24be3659d30613bd409c78ed5e"),
    # 2026-10-22: "No deduplicated credit" cells and the model-disagreement ranking guard.
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    (7, "b8edba16df38e92d5999f775e49e78264c85ef24be3659d30613bd409c78ed5e"),
)


def test_a_scorecard_tone_or_scale_change_bumps_its_render_format() -> None:
    """The Scorecard's own roster (ADR-121) is tracked like the Campaigns and Retail health one."""
    rows = sorted(
        [name, metric.favourable.value, metric.scale.value] for name, metric in SEMANTICS.metrics
    )
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    assert (scorecard_source.SCORECARD_PRESET_RENDER_FORMAT, digest) == (
        _SCORECARD_BAKED_SEMANTICS[-1]
    )
    assert [name for name, _ in SEMANTICS.metrics] == sorted(SCORECARD_COMPARED_METRICS)


# --- UI slice 9a: calendar presets in the materializer (A3) --------------------------------

SCORECARD_POLICY = PresentationTemporalPolicy(
    candidate_presets=(
        TemporalPresetId.SEVEN_DAYS,
        TemporalPresetId.THIRTY_DAYS,
        TemporalPresetId.LAST_COMPLETE_WEEK,
        TemporalPresetId.LAST_COMPLETE_MONTH,
    ),
    default_preset=TemporalPresetId.SEVEN_DAYS,
    custom_enabled=False,
    custom_min_duration_days=1,
    custom_max_duration_days=90,
    comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
    display_grains=(PageTime.PERIOD,),
)
SEPTEMBER_16 = datetime(2026, 9, 16, 12, tzinfo=UTC)


@dataclass
class _Coverage:
    intervals: tuple[DateRange, ...]

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        del request
        return SourceCoverageResult(
            contributors=(),
            bounds=SourceCoverageBounds(
                intervals=self.intervals,
                earliest_start=None if not self.intervals else self.intervals[0].start,
                latest_end=None if not self.intervals else self.intervals[-1].end,
            ),
            latest_loaded_at=None,
        )


@dataclass
class _Pointers:
    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        del page, variant
        return None


@dataclass
class _Compiler:
    received: list[ScorecardRenderInput] = field(default_factory=list[ScorecardRenderInput])

    def render_scorecard(self, request: ScorecardRenderInput) -> ScorecardRenderResult:
        self.received.append(request)
        return _renderer(ScorecardQueries(request.period)).render_scorecard(request)


def _source(
    covered: tuple[DateRange, ...],
) -> tuple[RetainedScorecardPresetArtifactSource, _Compiler]:
    compiler = _Compiler()
    source = RetainedScorecardPresetArtifactSource(
        coverage=cast("SourceCoveragePort", _Coverage(covered)),
        coverage_requirements=scorecard_coverage_requirements(CATALOG),
        temporal_policy=SCORECARD_POLICY,
        compiler=compiler,
        pointers=cast("PresetArtifactPointerStorePort", _Pointers()),
        clock=lambda: SEPTEMBER_16,
    )
    return source, compiler


def test_a_calendar_preset_bakes_its_latest_covered_period_with_no_prior() -> None:
    covered = (DateRange(date(2026, 7, 1), date(2026, 9, 15)),)
    source, compiler = _source(covered)
    month = source.publication_for(PresetArtifactVariant("last_complete_month"))
    week = source.publication_for(PresetArtifactVariant("last_complete_week"))
    assert isinstance(month, PresetArtifactPublicationRequest)
    assert isinstance(week, PresetArtifactPublicationRequest)
    august, iso_week = compiler.received
    assert (august.period, august.prior_period) == (
        DateRange(date(2026, 8, 1), date(2026, 8, 31)),
        None,
    )
    assert (august.preset_label, august.period_label) == ("Latest measured month", "August 2026")
    # The latest complete ISO week ending on or before Sep 16 is Mon Sep 7 to Sun Sep 13.
    assert iso_week.period == DateRange(date(2026, 9, 7), date(2026, 9, 13))
    assert iso_week.prior_period is None
    pointer = month.pointer_advance.pointer
    assert (pointer.resolved_window, pointer.comparison_window) == (august.period, None)
    body = json.loads(month.body.payload_json)
    assert body["period_label"] == "August 2026"
    kinds = {item["finding_id"] for item in body["findings"]}
    assert "channel-roas-mover" not in kinds
    # Paired: the rolling preset on the same coverage still bakes with its equal prior.
    rolling = source.publication_for(PresetArtifactVariant("7d"))
    assert rolling is not None
    assert compiler.received[-1].prior_period is not None


def test_a_calendar_preset_with_no_complete_period_bakes_nothing() -> None:
    # August misses its first two days and July isn't covered: no complete month qualifies.
    source, compiler = _source((DateRange(date(2026, 8, 3), date(2026, 9, 15)),))
    assert source.publication_for(PresetArtifactVariant("last_complete_month")) is None
    assert compiler.received == []
    result = scorecard_calendar_period(
        TemporalPresetId.LAST_COMPLETE_MONTH,
        SEPTEMBER_16.date(),
        (DateRange(date(2026, 8, 3), date(2026, 9, 15)),),
    )
    assert result.period is None
    assert result.none_text is not None
    assert result.none_text.startswith("No complete month has been measured yet.")
    # Paired: the same source bakes once August is covered end to end.
    covered, _ = _source((DateRange(date(2026, 8, 1), date(2026, 9, 15)),))
    assert covered.publication_for(PresetArtifactVariant("last_complete_month")) is not None


# --- UI slice 9a: insight cards and "Also changed" (A5) ------------------------------------


def _rule(  # noqa: PLR0913, PLR0917 -- one rule's five governed fields
    rule_id: str, finding: str, channel: str, direction: str, threshold: str
) -> AlertRule:
    return AlertRule(
        rule_id,
        "network-scorecard",
        finding,
        AlertRuleScope("amazon", "amazon_ads", channel),
        AlertDirection(direction),
        Decimal(threshold),
        InsightHorizonRole.TACTICAL_REVIEW,
    )


def _with_rules(*rules: AlertRule) -> NetworkScorecardArtifact:
    renderer = replace(_renderer(ScorecardQueries(PERIOD)), alert_rules=rules)
    result = renderer.render_scorecard(ScorecardRenderInput(PERIOD, PRIOR))
    assert result.artifact is not None
    return result.artifact


def test_each_narrated_finding_is_a_card_written_from_its_own_channel() -> None:
    cards = _rendered().insight_cards
    assert [(card.kind_label, card.headline) for card in cards] == [
        ("Channel mover", "Sponsored Products ROAS rose 0.50\u00d7"),
        ("Spend share", "Sponsored Products took 60% of Amazon spend"),
        ("Spend share", "Amazon DSP took 40% of Amazon spend"),
    ]
    assert cards[0].body == (
        "Spend rose 20.0% while attributed revenue rose, inside Sponsored Products only."
    )
    # DSP held 400 of 900 in the prior window: 44%.
    assert cards[2].body == "Down from 44% in Aug 6\u201312, inside the Amazon network."


def test_also_changed_lists_a_rule_met_skips_a_narrated_finding_and_is_absent_when_empty() -> None:
    # The committed defaults meet nothing on these figures: no section is served.
    default = network_scorecard_artifact_payload(
        _with_rules(*scorecard_source.scorecard_alert_rules(ROOT))
    )
    assert default["also_changed"] is None
    # A rule over Sponsored Products' +20% spend move lists one direction-only row.
    listed = _with_rules(_rule("sp-spend", "channel-spend-mover", SP, "up", "10"))
    (row,) = listed.also_changed
    assert (row.label, row.direction_text, row.window_text, row.scope_text) == (
        "Sponsored Products ad spend",
        "Up",
        "Aug 13\u201319 vs Aug 6\u201312",
        "inside Sponsored Products only",
    )
    payload = network_scorecard_artifact_payload(listed)
    rows = cast("dict[str, list[dict[str, object]]]", payload["also_changed"])["rows"]
    assert set(rows[0]) == {
        "rule_id",
        "finding_id",
        "card_id",
        "channel",
        "label",
        "marketplace_text",
        "title",
        "direction_text",
        "window_text",
        "scope_text",
        "dialog_text",
    }
    # One marketplace: the row names none, and its name is its label (SC9A-GATE-001).
    assert (rows[0]["marketplace_text"], rows[0]["title"]) == (None, rows[0]["label"])
    # The row's dialog narration is the server's sentence, never composed in the browser (G9A-3).
    assert rows[0]["dialog_text"] == (
        f"{rows[0]['direction_text']} {rows[0]['window_text']} \u00b7 {rows[0]['scope_text']}"
    )
    # A rule over Sponsored Products' ROAS rise is met, but its card already narrates it.
    narrated = _with_rules(_rule("sp-roas-up", "channel-roas-mover", SP, "up", "5"))
    assert narrated.also_changed == ()
    # The wrong direction or another channel's scope lists nothing.
    down = _rule("sp-spend-down", "channel-spend-mover", SP, "down", "10")
    assert _with_rules(down).also_changed == ()
    other = _rule("dsp-spend", "channel-spend-mover", DSP, "up", "10")
    assert _with_rules(other).also_changed == ()
