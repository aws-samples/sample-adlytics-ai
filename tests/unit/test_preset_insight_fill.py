# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913, PLR0917
"""Best-effort §5.3 insight-fill child behavior on preset pointer advance."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from bake_application_support import legacy_application, remaining_ms
from campaign_portfolio_support import LABELS, ONE_CHANNEL_FILTER
from change_support import SEMANTICS
from insight_key_support import PENDING_INPUT_DIGEST, with_card_digest
from preset_artifact_support import published_result, template_of

import adapters.jobs.preset_artifact_publisher.insight_fill as insight_fill_module
from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    PresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.insight_fill import PresetInsightFillChild
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent, DeclaredBakeIntents
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CampaignListEvidence,
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
    CampaignPortfolioArtifact,
    CampaignPortfolioComparison,
    CampaignPortfolioMetric,
    CampaignPortfolioMetricValue,
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
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCacheKey,
    InsightContinuationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlotId,
    InsightWindowProfile,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange
from ports.outbound.preset_artifacts import PresetArtifactPersistencePort

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074", "ADR-068")]


@dataclass
class Prompts:
    active_versions: dict[InsightSlotId, str] = field(default_factory=dict[InsightSlotId, str])

    def active(
        self, slot_id: InsightSlotId, tier: InsightPromptTier
    ) -> InsightPromptActivation | None:
        del tier
        version = self.active_versions.get(slot_id)
        if version is None:
            return None
        return InsightPromptActivation(
            slot_id, version, "A reviewed fictional prompt.", InsightModelRoute.QUICK
        )

    def version(self, slot_id: InsightSlotId, version: str) -> InsightPromptActivation | None:
        del slot_id, version
        return None


@dataclass
class Continuation:
    requests: list[InsightContinuationRequest] = field(
        default_factory=list[InsightContinuationRequest]
    )
    fail: bool = False

    def enqueue(self, request: InsightContinuationRequest) -> str:
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.requests.append(request)
        return f"message-{len(self.requests)}"


def _publication_request(page: str) -> PresetArtifactPublicationRequest:
    revision = PresetArtifactRevision.from_publication_identities(("publication-fictional",))
    body = PresetArtifactBody(
        PresetArtifactPage(page),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json({"kind": "fictional"}),
    )
    pointer = PresetArtifactPointer(body.page, body.variant, revision, body.digest, 1)
    return PresetArtifactPublicationRequest(
        body, template_of(PresetArtifactPointerAdvanceRequest(pointer))
    )


def _assembly(slot_id: InsightSlotId) -> InsightAssemblyRequest:
    return with_card_digest(_pending_assembly(slot_id))


def _pending_assembly(slot_id: InsightSlotId) -> InsightAssemblyRequest:
    return InsightAssemblyRequest(
        key=InsightCacheKey(
            analytical_scope_id="analytical-scope-v1:fictional",
            subject="campaign-portfolio",
            window_start="2026-09-01",
            window_end="2026-09-07",
            artifact_revision="revision-fictional",
            prompt_version="active-pointer",
            slot_id=slot_id,
            input_digest=PENDING_INPUT_DIGEST,
        ),
        output_class=InsightOutputClass.PERSPECTIVE,
        purpose="Explain one server-owned portfolio movement.",
        findings=(DeterministicFinding("Fictional Campaign", Decimal("5.00")),),
        silence_reason="No fictional portfolio movement exceeds the declared threshold.",
        window_profile=InsightWindowProfile(
            days=7,
            grain=TemporalDisplayGrain.DAY,
            prior_start="2026-08-25",
            prior_end="2026-08-31",
            role=InsightHorizonRole.TACTICAL_REVIEW,
        ),
    )


def _patched_derivation(monkeypatch: pytest.MonkeyPatch, calls: list[object]) -> None:
    def decode(payload: object) -> object:
        calls.append(payload)
        return SimpleNamespace(kind="fictional-decoded")

    def derive(*, artifact: object, scope: object, contracts: object) -> tuple[object, tuple[()]]:
        del artifact, scope, contracts
        return (
            SimpleNamespace(
                requests=(
                    _assembly(InsightSlotId.CONTRIBUTION),
                    _assembly(InsightSlotId.DIVERGENCE),
                )
            ),
            (),
        )

    monkeypatch.setattr(insight_fill_module, "campaign_portfolio_artifact_from_payload", decode)
    monkeypatch.setattr(insight_fill_module, "portfolio_insight_request_derivation", derive)


def _patched_retail_derivation(monkeypatch: pytest.MonkeyPatch, calls: list[object]) -> None:
    def decode(payload: object) -> object:
        calls.append(payload)
        return SimpleNamespace(kind="fictional-retail-decoded")

    def derive(request: object) -> object:
        del request
        return SimpleNamespace(
            requests=(
                _assembly(InsightSlotId.DEMAND_SHAPE),
                _assembly(InsightSlotId.ORDERED_VS_SHIPPED),
            )
        )

    monkeypatch.setattr(insight_fill_module, "retail_health_artifact_from_payload", decode)
    monkeypatch.setattr(insight_fill_module, "retail_insight_request_derivation", derive)


def _child(prompts: Prompts, continuation: Continuation) -> PresetInsightFillChild:
    return PresetInsightFillChild(
        prompts=cast("insight_fill_module.InsightPromptActivationPort", prompts),
        continuation=cast("insight_fill_module.InsightContinuationPort", continuation),
        scope=cast("insight_fill_module.AuthorizedAnalyticalScope", object()),
        contracts={},
        catalog=cast("insight_fill_module.Catalog", object()),
    )


def test_warm_enqueues_each_admitted_slot_with_its_immutable_active_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child admits the exact serve-time first-fill continuation envelope.

    The worker therefore warms byte-identical revision-keyed cache entries.
    """
    calls: list[object] = []
    _patched_derivation(monkeypatch, calls)
    prompts = Prompts({InsightSlotId.CONTRIBUTION: "v7", InsightSlotId.DIVERGENCE: "v3"})
    continuation = Continuation()

    _child(prompts, continuation).warm(_publication_request("campaigns"))

    assert [item.generation.key.slot_id for item in continuation.requests] == [
        InsightSlotId.CONTRIBUTION,
        InsightSlotId.DIVERGENCE,
    ]
    assert [item.generation.key.prompt_version for item in continuation.requests] == ["v7", "v3"]
    assert len(calls) == 1


def test_warm_skips_slots_without_an_active_prompt_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_derivation(monkeypatch, [])
    prompts = Prompts({InsightSlotId.CONTRIBUTION: "v7"})
    continuation = Continuation()

    _child(prompts, continuation).warm(_publication_request("campaigns"))

    assert [item.generation.key.slot_id for item in continuation.requests] == [
        InsightSlotId.CONTRIBUTION
    ]


def test_warm_swallows_every_failure_because_the_child_never_gates_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_derivation(monkeypatch, [])
    continuation = Continuation(fail=True)

    _child(Prompts({InsightSlotId.CONTRIBUTION: "v7"}), continuation).warm(
        _publication_request("campaigns")
    )

    assert continuation.requests == []


def test_warm_ignores_pages_that_declare_no_preset_served_model_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    _patched_derivation(monkeypatch, calls)
    _patched_retail_derivation(monkeypatch, calls)
    continuation = Continuation()

    _child(Prompts(), continuation).warm(_publication_request("channels"))

    assert calls == []
    assert continuation.requests == []


def test_warm_derives_retail_slots_from_the_baked_retail_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retail page warms the same revision-keyed entries its serve overlay reads."""
    calls: list[object] = []
    _patched_retail_derivation(monkeypatch, calls)
    prompts = Prompts({InsightSlotId.DEMAND_SHAPE: "v2", InsightSlotId.ORDERED_VS_SHIPPED: "v5"})
    continuation = Continuation()

    _child(prompts, continuation).warm(_publication_request("retail-health"))

    assert [item.generation.key.slot_id for item in continuation.requests] == [
        InsightSlotId.DEMAND_SHAPE,
        InsightSlotId.ORDERED_VS_SHIPPED,
    ]
    assert [item.generation.key.prompt_version for item in continuation.requests] == ["v2", "v5"]
    assert len(calls) == 1


@dataclass
class _RecordingChild:
    warmed: list[PresetArtifactPublicationRequest] = field(
        default_factory=list[PresetArtifactPublicationRequest]
    )

    def warm(self, request: PresetArtifactPublicationRequest) -> None:
        self.warmed.append(request)


@dataclass
class _Baker:
    result: BakePublication

    def bake(self, intent: BakeIntent) -> BakePublication:
        del intent
        return self.result


@dataclass
class _Persistence:
    outcome: PresetArtifactPointerAdvanceOutcome

    def publish(
        self, request: PresetArtifactPublicationRequest, **_deadline: object
    ) -> PresetArtifactPublicationResult:
        return published_result(request, self.outcome)


@pytest.mark.parametrize(
    ("outcome", "expected_warms"),
    [
        (PresetArtifactPointerAdvanceOutcome.ADVANCED, 1),
        (PresetArtifactPointerAdvanceOutcome.NO_OP, 0),
    ],
)
def test_bake_application_warms_only_after_a_real_pointer_advance(
    outcome: PresetArtifactPointerAdvanceOutcome, expected_warms: int
) -> None:
    intent = BakeIntent("campaigns", "7d")
    request = _publication_request("campaigns")
    child = _RecordingChild()
    application = legacy_application(
        declared=DeclaredBakeIntents((intent,)),
        baker=cast("PresetArtifactBaker", _Baker(BakePublication(request))),
        persistence=cast("PresetArtifactPersistencePort", _Persistence(outcome)),
        insight_fill=cast("PresetInsightFillChild", child),
    )

    application.apply(intent.serialize(), remaining_ms=remaining_ms, final=False)

    assert len(child.warmed) == expected_warms


# --- end-to-end key identity: the child's real derivation versus the serve path's ---

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 3))
PRIOR = DateRange(date(2026, 7, 29), date(2026, 7, 31))
USD = Units(UnitKind.CURRENCY, "USD")
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)
ROOT = Path(__file__).resolve().parents[2]


def _metric_value(
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


def _metric_comparison(
    metric: CampaignPortfolioMetric,
    current: str | int,
    prior: str | int,
    units: Units,
    grain: object,
) -> CampaignPortfolioComparison:
    # Every two-sided comparison carries its governed change block (ADR-121).
    return campaign_portfolio_comparison(
        metric,
        _metric_value(current, units, PERIOD, grain),
        _metric_value(prior, units, PRIOR, grain),
        SEMANTICS,
    )


def _portfolio_row(index: int) -> CampaignPortfolioRow:
    signature = SafeCampaignDisplaySignature(
        effective_name=f"Fictional Flight {index:03d}",
        channel="Fictional channel",
        marketplace="United States",
        platform="Amazon Ads",
        effective_name_status=EffectiveNameStatus.AVAILABLE,
        effective_name_valid_from=date(2026, 1, 1),
    )
    comparisons = (
        _metric_comparison(
            CampaignPortfolioMetric.SPEND,
            str(100 * (index + 1)),
            "5",
            USD,
            CAMPAIGN_PORTFOLIO_ROW_GRAIN,
        ),
        _metric_comparison(
            CampaignPortfolioMetric.ATTRIBUTED_ROAS, "2.5", "2", RATIO, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
        _metric_comparison(
            CampaignPortfolioMetric.PURCHASES, index + 10, 1, COUNT, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
        _metric_comparison(
            CampaignPortfolioMetric.CTR, "0.25", "0.2", RATIO, CAMPAIGN_PORTFOLIO_ROW_GRAIN
        ),
    )
    return CampaignPortfolioRow(
        signature,
        CampaignSelectionCapability(f"selection-{index:03d}", datetime(2026, 9, 1, tzinfo=UTC)),
        comparisons,
        ("fresh",),
    )


def _fictional_portfolio() -> CampaignPortfolioArtifact:
    totals = (
        _metric_comparison(
            CampaignPortfolioMetric.SPEND, "1000", "900", USD, CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
        ),
        _metric_comparison(
            CampaignPortfolioMetric.PURCHASES, 100, 90, COUNT, CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
        ),
        _metric_comparison(
            CampaignPortfolioMetric.IMPRESSIONS, 10000, 9000, COUNT, CAMPAIGN_PORTFOLIO_TOTAL_GRAIN
        ),
        _metric_comparison(
            CampaignPortfolioMetric.PORTFOLIO_ROAS,
            "2.5",
            "2",
            RATIO,
            CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
        ),
    )
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
    return build_campaign_portfolio(
        period=PERIOD,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        rows=tuple(_portfolio_row(index) for index in range(2)),
        totals=totals,
        trend=trend,
        evidence=(CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),),
        # A served render-format-11 body: a one-channel sibling with its coverage block.
        evidence_status=campaign_portfolio_evidence_status(PERIOD, LABELS),
        channel_filter=ONE_CHANNEL_FILTER,
    )


def test_child_warms_exactly_the_cache_keys_the_serve_path_would_derive() -> None:
    """The claim §5.3 rests on: warmed keys are byte-identical to serve-time first-fill.

    Runs the child against a real baked payload with the real shared derivation —
    nothing monkeypatched — and compares against a direct serve-style derivation of
    the same decoded artifact with the same active prompt replacement.
    """
    artifact = _fictional_portfolio()
    scope = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("fictional-scope")}))
    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    prompts = Prompts(
        dict.fromkeys(
            (InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE, InsightSlotId.EXPOSURE), "v9"
        )
    )
    continuation = Continuation()
    revision = PresetArtifactRevision.from_publication_identities(("publication-fictional",))
    body = PresetArtifactBody(
        PresetArtifactPage("campaigns"),
        PresetArtifactVariant("7d"),
        revision,
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact)),
    )
    request = PresetArtifactPublicationRequest(
        body, PresetArtifactPointerTemplate(body.page, body.variant, revision, body.digest, 1)
    )
    child = PresetInsightFillChild(
        prompts=cast("insight_fill_module.InsightPromptActivationPort", prompts),
        continuation=cast("insight_fill_module.InsightContinuationPort", continuation),
        scope=scope,
        contracts=contracts,
        catalog=cast("insight_fill_module.Catalog", object()),
    )

    child.warm(request)

    serve_derivation, _ = portfolio_insight_request_derivation(
        artifact=campaign_portfolio_artifact_from_payload(json.loads(body.payload_json)),
        scope=scope,
        contracts=contracts,
    )
    expected_keys = [
        replace(assembly.key, prompt_version="v9") for assembly in serve_derivation.requests
    ]
    warmed_keys = [item.generation.key for item in continuation.requests]
    assert warmed_keys, "the fictional portfolio must admit at least one declared slot"
    assert warmed_keys == expected_keys
    assert all(key.subject == "campaign-portfolio" for key in warmed_keys)
    assert all(
        item.generation.findings == assembly.findings
        for item, assembly in zip(continuation.requests, serve_derivation.requests, strict=True)
    )
