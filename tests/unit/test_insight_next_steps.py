# SPDX-License-Identifier: MIT-0
"""FR-11 card next steps (NextStep.v2), shipped dormant, and the context they need.

ADR-122: typed object targets checked against the routed tabs as options are built; cached
steps hold campaign keys, and the serve path maps each to a capability or drops the step.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from insight_key_support import PENDING_INPUT_DIGEST, card_key

from adapters.insight_next_steps import (
    ASSISTANT_STEP_LABEL,
    DATA_COMPLETE,
    EVIDENCE_STEP_LABEL,
    SURFACED_SLOTS,
    NextStepCampaign,
    NextStepSurface,
    available_next_steps_entry,
    comparison_window_entry,
    data_sufficiency_entry,
    next_step_options,
)
from adapters.jobs.contract_validation.insight_loader import (
    insight_contract_from_mapping,
    load_insight_contracts,
)
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.outbound.bedrock.insights import (
    CARD_NEXT_STEP_SCHEMA_ID,
    BedrockInsightGenerator,
    InsightModelRouteModels,
    card_envelope,
)
from domain.data_contracts.contract import ContractError
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    DeterministicFinding,
    InsightCacheKey,
    InsightGeneratedNarration,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightNextStep,
    InsightNextStepKind,
    InsightNextStepOption,
    InsightOutputClass,
    InsightPromptActivation,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    InsightWindowProfile,
    NextStepServeContext,
    insight_slots_from_payload,
    insight_slots_payload,
    next_step_from_payload,
    next_step_payload,
    resolve_next_step,
    served_insight_slots,
    served_next_step,
)
from domain.presentation.next_steps import (
    AssistantTarget,
    CampaignKeyTarget,
    CampaignRefTarget,
    MetricFocusTarget,
    NextStepPageTargets,
    NextStepTargetError,
    NextStepTargets,
    PageTarget,
    PresetTarget,
    assistant_question_refusal,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076", "ADR-122")]
ROOT = Path(__file__).resolve().parents[2]
CARD_KINDS = tuple(
    kind for kind in InsightNextStepKind if kind is not InsightNextStepKind.OPEN_PAGE
)
"""Every kind a card contract may list; open_page steps are declared by their page."""
_PROFILE = InsightWindowProfile(
    days=7,
    grain=TemporalDisplayGrain.DAY,
    prior_start="2026-09-22",
    prior_end="2026-09-28",
    role=InsightHorizonRole.TACTICAL_REVIEW,
)


WINDOW = "Sep 23\u201329, 2026"
QUESTION = f"What moved ad spend in {WINDOW}?"
TARGETS = NextStepTargets(
    {
        "campaigns": NextStepPageTargets(
            "campaigns",
            presets=frozenset(preset.value for preset in TemporalPresetId),
            filters=frozenset({"amazon_dsp"}),
            sections=frozenset({"campaign-catalog"}),
        ),
        "retail-health": NextStepPageTargets(
            "retail-health", frozenset({"7d", "14d"}), frozenset(), frozenset()
        ),
    },
    frozenset({"amazon_dsp", "amazon_sponsored_brands"}),
)
"""Fictional declared targets: a routed Campaigns tab with every preset and one filter."""


def _surface(**changes: object) -> NextStepSurface:
    fields: dict[str, Any] = {
        "targets": TARGETS,
        "page_id": "campaigns",
        "portfolio": True,
        "current_preset": TemporalPresetId.SEVEN_DAYS,
        "available_presets": tuple(TemporalPresetId),
        "campaigns": tuple(
            NextStepCampaign(f"Fictional Campaign {index}", f"campaign-key-{index:05d}")
            for index in range(3)
        ),
        "metrics": (("spend", "ad spend"), ("ctr", "click-through rate"), ("cpc", "CPC")),
        "evidence_complete": False,
        "assistant_question": QUESTION,
        "window_label": WINDOW,
        "cross_channel": False,
        "association_only": False,
    }
    fields.update(changes)
    return NextStepSurface(**fields)


def _kinds(options: tuple[InsightNextStepOption, ...]) -> list[InsightNextStepKind]:
    return [option.kind for option in options]


# Supplier ----------------------------------------------------------------------------------


def test_no_allowed_kind_builds_no_options_and_no_entry() -> None:
    assert next_step_options(_surface(), ()) == ()
    assert next_step_options(None, ()) == ()
    assert available_next_steps_entry(()) is None


def test_allowed_kinds_without_a_served_surface_is_a_wiring_defect() -> None:
    with pytest.raises(ValueError, match="requires its served surface"):
        next_step_options(None, (InsightNextStepKind.ASK_ASSISTANT,))


def test_the_largest_portfolio_case_has_exactly_six_options_in_quota_order() -> None:
    options = next_step_options(_surface(), CARD_KINDS)

    assert [option.step_id for option in options] == [f"s{index}" for index in range(1, 7)]
    assert _kinds(options) == [
        InsightNextStepKind.OPEN_PRESET,
        InsightNextStepKind.OPEN_CAMPAIGN_DETAIL,
        InsightNextStepKind.OPEN_CAMPAIGN_DETAIL,
        InsightNextStepKind.OPEN_METRIC_FOCUS,
        InsightNextStepKind.OPEN_EVIDENCE_STATUS,
        InsightNextStepKind.ASK_ASSISTANT,
    ]
    assert options[0] == InsightNextStepOption(
        "s1", InsightNextStepKind.OPEN_PRESET, PresetTarget("campaigns", "14d"), "Show 14 days"
    )
    assert options[1].label == "Open Fictional Campaign 0"
    assert options[1].target == CampaignKeyTarget("campaigns", "campaign-key-00000")
    assert options[3].label == "Open ad spend by day"
    assert options[3].target == MetricFocusTarget("campaigns", "spend")
    assert options[5].target == AssistantTarget(QUESTION)
    entry = available_next_steps_entry(options)
    assert entry is not None and entry.name == "available next steps"
    assert entry.value.startswith("s1: Show 14 days; s2: Open Fictional Campaign 0")


def test_the_largest_other_surface_case_has_exactly_five_options() -> None:
    options = next_step_options(_surface(portfolio=False), CARD_KINDS)

    assert len(options) == 5
    assert InsightNextStepKind.OPEN_CAMPAIGN_DETAIL not in _kinds(options)
    assert _kinds(options).count(InsightNextStepKind.OPEN_METRIC_FOCUS) == 2


def test_evidence_and_assistant_steps_follow_their_rules() -> None:
    qualified = next_step_options(_surface(), CARD_KINDS)
    complete = next_step_options(_surface(evidence_complete=True), CARD_KINDS)

    assert any(option.label == EVIDENCE_STEP_LABEL for option in qualified)
    assert not any(option.kind is InsightNextStepKind.OPEN_EVIDENCE_STATUS for option in complete)
    for options in (qualified, complete):
        assert options[-1].kind is InsightNextStepKind.ASK_ASSISTANT
        assert options[-1].label == ASSISTANT_STEP_LABEL


def test_only_allowed_and_served_kinds_appear() -> None:
    options = next_step_options(
        _surface(campaigns=(), available_presets=(TemporalPresetId.SEVEN_DAYS,)),
        (InsightNextStepKind.OPEN_PRESET, InsightNextStepKind.OPEN_CAMPAIGN_DETAIL),
    )
    assert options == ()


@pytest.mark.parametrize(
    ("current", "available", "target"),
    [
        (TemporalPresetId.ONE_DAY, tuple(TemporalPresetId), "7d"),
        (TemporalPresetId.FOURTEEN_DAYS, tuple(TemporalPresetId), "30d"),
        (TemporalPresetId.THIRTY_DAYS, tuple(TemporalPresetId), "14d"),
        (
            TemporalPresetId.FOURTEEN_DAYS,
            (TemporalPresetId.ONE_DAY, TemporalPresetId.SEVEN_DAYS, TemporalPresetId.FOURTEEN_DAYS),
            "7d",
        ),
    ],
)
def test_the_preset_step_opens_the_next_longer_or_from_the_longest_the_next_shorter(
    current: TemporalPresetId, available: tuple[TemporalPresetId, ...], target: str
) -> None:
    options = next_step_options(
        _surface(current_preset=current, available_presets=available),
        (InsightNextStepKind.OPEN_PRESET,),
    )
    assert [option.target for option in options] == [PresetTarget("campaigns", target)]


def test_a_step_to_an_undeclared_target_fails_loading() -> None:
    """ADR-122 decision 5: a target nothing declares fails as the options are built."""
    undeclared = (
        _surface(page_id="channel-performance"),
        _surface(targets=NextStepTargets({}, frozenset())),
        _surface(
            targets=NextStepTargets(
                {
                    "campaigns": NextStepPageTargets(
                        "campaigns", frozenset({"7d"}), frozenset(), frozenset()
                    )
                }
            )
        ),
    )
    for surface in undeclared:
        with pytest.raises(NextStepTargetError):
            next_step_options(surface, (InsightNextStepKind.OPEN_PRESET,))
    for target in (
        PageTarget("prompt-admin"),
        PageTarget("campaigns", preset_id="90d"),
        PageTarget("campaigns", filter_id="amazon_sponsored_display"),
        PageTarget("campaigns", section_id="channel-table"),
        PageTarget("campaigns", highlight_channel="amazon_sponsored_display"),
        MetricFocusTarget("retail-health", "spend", filter_id="amazon_dsp"),
    ):
        with pytest.raises(NextStepTargetError):
            TARGETS.check(target)
    for target in (
        None,
        AssistantTarget(QUESTION),
        PageTarget(
            "campaigns",
            preset_id="14d",
            filter_id="amazon_dsp",
            section_id="campaign-catalog",
            highlight_channel="amazon_sponsored_brands",
        ),
        CampaignKeyTarget("campaigns", "campaign-key-00000"),
    ):
        TARGETS.check(target)


def test_the_routed_tabs_are_the_declared_pages_with_navigation() -> None:
    """Every shipped presentation tab is a declared page with navigation."""
    targets = NextStepTargets.from_page_schemas(
        load_v2_page_schemas(ROOT), filters={"campaigns": frozenset({"all", "amazon_dsp"})}
    )
    assert set(targets.pages) == {
        "audiences",
        "campaigns",
        "new-customers",
        "reach",
        "retail-health",
        "retention",
        "scorecard",
    }
    # A vintage page declares no presets, so no step may name one on Retention.
    assert targets.pages["retention"].presets == frozenset()
    for calendar in ("scorecard", "new-customers"):
        assert targets.pages[calendar].presets == frozenset(
            {"7d", "30d", "last_complete_week", "last_complete_month"}
        )
    assert targets.pages["campaigns"].presets == frozenset({"1d", "7d", "14d", "30d"})
    assert targets.pages["campaigns"].filters == frozenset({"all", "amazon_dsp"})
    assert targets.pages["retail-health"].filters == frozenset()
    assert "campaign-catalog" in targets.pages["campaigns"].sections
    targets.check(MetricFocusTarget("campaigns", "spend", "amazon_dsp"))
    with pytest.raises(NextStepTargetError):
        targets.check(MetricFocusTarget("campaigns", "spend", "amazon_sponsored_television"))
    with pytest.raises(NextStepTargetError):
        targets.check(PresetTarget("channel-performance", "7d"))
    with pytest.raises(NextStepTargetError):
        targets.check(PresetTarget("retail-health", "30d"))


@pytest.mark.parametrize(
    ("question", "flags", "refusal"),
    [
        (QUESTION, (False, False), None),
        (QUESTION, (True, True), None),
        ("What moved ad spend this week?", (False, False), "undated"),
        ("x" * 241, (False, False), "length"),
        (
            f"Why did total ROAS across channels fall in {WINDOW}?",
            (True, False),
            "cross_channel_attributed_total",
        ),
        (f"Why did total ROAS across channels fall in {WINDOW}?", (False, False), None),
        (f"What drove reach in {WINDOW}?", (False, True), "causal_verb"),
        (f"What drove ad spend in {WINDOW}?", (False, False), None),
        (f"What is reach associated with in {WINDOW}?", (False, True), None),
    ],
)
def test_an_assistant_question_follows_the_text_rules(
    question: str, flags: tuple[bool, bool], refusal: str | None
) -> None:
    """ADR-122 decision 2: dated, no cross-channel attributed total, no causal E1/D2 verb."""
    cross_channel, association_only = flags
    assert (
        assistant_question_refusal(
            question,
            window_label=WINDOW,
            cross_channel=cross_channel,
            association_only=association_only,
        )
        == refusal
    )


def test_a_refused_question_drops_the_assistant_step() -> None:
    for surface in (
        _surface(assistant_question=None),
        _surface(assistant_question="What moved ad spend this week?"),
        _surface(assistant_question=f"What drove reach in {WINDOW}?", association_only=True),
    ):
        options = next_step_options(surface, (InsightNextStepKind.ASK_ASSISTANT,))
        assert options == ()


_CONTEXT = NextStepServeContext(
    campaign_refs={f"campaign-key-{index:05d}": f"capability-{index}" for index in range(3)},
    offered_filters=frozenset({"amazon_dsp"}),
)


def _cached(target: object, kind: InsightNextStepKind) -> InsightNextStep:
    return InsightNextStep(kind, cast("Any", target), "Open", "A reason.")


def test_a_step_whose_campaign_has_no_capability_is_dropped() -> None:
    """ADR-122 decision 4: the key maps to this response's capability, or the step drops."""
    kind = InsightNextStepKind.OPEN_CAMPAIGN_DETAIL
    minted = _cached(CampaignKeyTarget("campaigns", "campaign-key-00001"), kind)
    unminted = _cached(CampaignKeyTarget("campaigns", "campaign-key-99999"), kind)
    served = served_next_step(minted, _CONTEXT)
    assert served is not None
    assert served.target == CampaignRefTarget("campaigns", "capability-1")
    assert next_step_payload(served)["target"] == {
        "page_id": "campaigns",
        "campaign_ref": "capability-1",
    }
    assert served_next_step(unminted, _CONTEXT) is None
    assert served_next_step(minted, NextStepServeContext()) is None
    slots = served_insight_slots((_filled(minted), _filled(unminted), _filled(None)), _CONTEXT)
    assert [slot.next_step for slot in slots] == [served, None, None]
    assert all(slot.text == "A fictional reading." for slot in slots)


def test_a_step_whose_filter_is_not_offered_is_dropped() -> None:
    """ADR-122 decision 5: no chip opens a filter this workspace doesn't serve."""
    offered = _cached(
        MetricFocusTarget("campaigns", "spend", "amazon_dsp"), InsightNextStepKind.OPEN_METRIC_FOCUS
    )
    page = _cached(PageTarget("campaigns", filter_id="amazon_dsp"), InsightNextStepKind.OPEN_PAGE)
    assert served_next_step(offered, _CONTEXT) == offered
    assert served_next_step(page, _CONTEXT) == page
    for context in (NextStepServeContext(), NextStepServeContext(offered_filters=frozenset({"x"}))):
        assert served_next_step(offered, context) is None
        assert served_next_step(page, context) is None
    unfiltered = _cached(PresetTarget("campaigns", "14d"), InsightNextStepKind.OPEN_PRESET)
    assert served_next_step(unfiltered, NextStepServeContext()) == unfiltered


def test_a_campaign_key_is_never_served_and_a_capability_never_cached() -> None:
    kind = InsightNextStepKind.OPEN_CAMPAIGN_DETAIL
    keyed = _cached(CampaignKeyTarget("campaigns", "campaign-key-00001"), kind)
    referenced = _cached(CampaignRefTarget("campaigns", "capability-1"), kind)
    with pytest.raises(ValueError, match="server-only"):
        next_step_payload(keyed)
    with pytest.raises(ValueError, match="never cached"):
        next_step_payload(referenced, cached=True)
    with pytest.raises(ValueError, match="cannot already hold a capability"):
        served_next_step(referenced, _CONTEXT)
    with pytest.raises(ValueError, match="never a capability"):
        InsightNextStepOption("s1", kind, CampaignRefTarget("campaigns", "capability-1"), "Open")
    wire = next_step_payload(referenced)
    with pytest.raises(ValueError, match="not exact"):
        next_step_from_payload(wire, cached=True)
    with pytest.raises(ValueError, match="not exact"):
        next_step_from_payload(next_step_payload(keyed, cached=True))


def _filled(step: InsightNextStep | None) -> InsightSlot:
    return InsightSlot(
        InsightSlotId.CONTRIBUTION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        "A fictional reading.",
        "Read from the values this page already shows.",
        "sha256:revision-fictional",
        "v1",
        next_step=step,
    )


# FR-11 context, active ----------------------------------------------------------------------


def test_the_comparison_window_names_its_length_in_customer_words() -> None:
    entry = comparison_window_entry(DateRange(date(2026, 9, 22), date(2026, 9, 28)))
    assert entry.value == "2026-09-22 to 2026-09-28, the 7 days before this view"


def test_data_sufficiency_is_complete_or_names_the_served_gaps() -> None:
    assert data_sufficiency_entry(()).value == DATA_COMPLETE
    gapped = data_sufficiency_entry(
        ("Latest day is preliminary", "Latest day is preliminary"),
        (DateRange(date(2026, 9, 30), date(2026, 9, 30)),),
    )
    assert gapped.value == (
        "This view's data is not complete: Latest day is preliminary; no data for 2026-09-30."
    )


# Domain -------------------------------------------------------------------------------------

_PRESET = PresetTarget("campaigns", "14d")
_OPTIONS = (
    InsightNextStepOption("s1", InsightNextStepKind.OPEN_PRESET, _PRESET, "Show 14 days"),
    InsightNextStepOption(
        "s2", InsightNextStepKind.OPEN_EVIDENCE_STATUS, None, EVIDENCE_STEP_LABEL
    ),
)


def test_a_step_resolves_by_id_and_refuses_an_id_outside_the_options() -> None:
    step = resolve_next_step(_OPTIONS, "s1", "The 14-day view shows whether the drop persists.")
    assert step == InsightNextStep(
        InsightNextStepKind.OPEN_PRESET,
        _PRESET,
        "Show 14 days",
        "The 14-day view shows whether the drop persists.",
    )
    with pytest.raises(ValueError, match="not one of this card's served options"):
        resolve_next_step(_OPTIONS, "s9", "A reason.")


def test_reason_bounds_are_structural_only() -> None:
    for reason in ("", "   ", "x" * 241):
        with pytest.raises(ValueError, match="at most 240"):
            resolve_next_step(_OPTIONS, "s1", reason)
    # A 240-character reason naming a figure absent from the findings is accepted (R5-2).
    reason = ("Spend fell 99.99% on Fictional Campaign 7. " * 6)[:240]
    assert resolve_next_step(_OPTIONS, "s1", reason).reason == reason


def test_a_silent_card_cannot_offer_a_step() -> None:
    step = resolve_next_step(_OPTIONS, "s2", "The data behind this view has gaps.")
    with pytest.raises(ValueError, match="silent insight narration cannot offer"):
        InsightGeneratedNarration(InsightOutputClass.PERSPECTIVE, (), step)
    with pytest.raises(ValueError, match="only a filled insight slot"):
        InsightSlot(
            InsightSlotId.CONTRIBUTION,
            InsightOutputClass.PERSPECTIVE,
            InsightSlotState.SILENT,
            None,
            None,
            "sha256:revision-fictional",
            "v1",
            next_step=step,
        )


def test_each_kind_round_trips_and_a_missing_or_extra_key_fails() -> None:
    for option in next_step_options(_surface(), CARD_KINDS):
        step = InsightNextStep(option.kind, option.target, option.label, "A reason.")
        cached = json.loads(json.dumps(next_step_payload(step, cached=True)))
        assert next_step_from_payload(cached, cached=True) == step
        served = served_next_step(step, _CONTEXT)
        assert served is not None
        assert next_step_from_payload(json.loads(json.dumps(next_step_payload(served)))) == served
    page = InsightNextStep(
        InsightNextStepKind.OPEN_PAGE,
        PageTarget("retail-health", preset_id="14d", section_id="products"),
        "Open Retail health",
        "A reason.",
    )
    assert next_step_payload(page)["target"] == {
        "page_id": "retail-health",
        "preset_id": "14d",
        "section_id": "products",
    }
    assert next_step_from_payload(next_step_payload(page)) == page
    payload = next_step_payload(resolve_next_step(_OPTIONS, "s1", "A reason."))
    for broken in ({k: v for k, v in payload.items() if k != "label"}, {**payload, "extra": 1}):
        with pytest.raises(ValueError, match="fields are not exact"):
            next_step_from_payload(broken)
    with pytest.raises(ValueError):
        next_step_from_payload({**payload, "kind": "open_budget_editor"})
    # v1's bare-string target and any extra target key are refused (ADR-122 decision 1).
    for target in ("14d", {"page_id": "campaigns", "preset_id": "14d", "metric": "spend"}, {}):
        with pytest.raises(ValueError):
            next_step_from_payload({**payload, "target": target})


def test_a_filled_slot_carries_its_step_and_a_legacy_slot_reads_as_none() -> None:
    step = resolve_next_step(_OPTIONS, "s1", "A reason.")
    filled = InsightSlot(
        InsightSlotId.CONTRIBUTION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        "A fictional reading.",
        "Read from the values this page already shows.",
        "sha256:revision-fictional",
        "v1",
        next_step=step,
    )
    legacy = InsightSlot(
        InsightSlotId.DIVERGENCE,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        "A fictional reading.",
        "Read from the values this page already shows.",
        "sha256:revision-fictional",
        "v1",
    )
    payload = insight_slots_payload((filled, legacy))
    assert "next_step" in payload[0]
    assert "next_step" not in payload[1]
    decoded = insight_slots_from_payload(
        payload, expected=(InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE)
    )
    assert decoded[0].next_step == step
    assert decoded[1].next_step is None


# Contracts ----------------------------------------------------------------------------------


def test_every_shipped_card_contract_allows_no_next_step() -> None:
    """R6-6: options and the prompt are switched on together, only after the gate."""
    contracts = [contract for _, contract in load_insight_contracts(ROOT)]
    model_cards = [contract for contract in contracts if contract.id not in DETERMINISTIC_SLOT_IDS]
    assert len(model_cards) == 12
    assert all(contract.next_step_kinds == () for contract in contracts)


def test_the_loader_refuses_an_unknown_kind_and_a_missing_declaration() -> None:
    path = ROOT / "contracts" / "insights" / "portfolio-contribution.yaml"
    raw = yaml.safe_load(path.read_text())
    allowed = insight_contract_from_mapping(
        {**raw, "next_steps": {"allowed_kinds": ["ask_assistant"]}}, where="fictional"
    )
    assert allowed.next_step_kinds == (InsightNextStepKind.ASK_ASSISTANT,)
    with pytest.raises((ContractError, ValueError), match="declared by their page"):
        insight_contract_from_mapping(
            {**raw, "next_steps": {"allowed_kinds": ["open_page"]}}, where="fictional"
        )
    with pytest.raises(ContractError, match="unsupported enum value"):
        insight_contract_from_mapping(
            {**raw, "next_steps": {"allowed_kinds": ["change_budget"]}}, where="fictional"
        )
    with pytest.raises(ContractError, match="fields are not exact"):
        insight_contract_from_mapping(
            {key: value for key, value in raw.items() if key != "next_steps"}, where="fictional"
        )
    with pytest.raises(ContractError, match="allowed_kinds"):
        insight_contract_from_mapping({**raw, "next_steps": {"kinds": []}}, where="fictional")


def test_a_kind_on_a_slot_without_a_served_surface_fails_loading(tmp_path: Path) -> None:
    """No derivation builds a surface yet, so turning a kind on fails at declaration load."""
    assert not SURFACED_SLOTS
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    path = tmp_path / "contracts" / "insights" / "portfolio-contribution.yaml"
    path.write_text(
        path.read_text().replace(
            "next_steps:\n  allowed_kinds: []", "next_steps:\n  allowed_kinds: [ask_assistant]"
        )
    )
    with pytest.raises(ContractError, match="no derivation builds a served next-step surface"):
        load_insight_contracts(tmp_path)


# Model envelope -----------------------------------------------------------------------------


def _generation(options: tuple[InsightNextStepOption, ...]) -> InsightGenerationRequest:
    model_input = SimpleNamespace(
        output_class=InsightOutputClass.PERSPECTIVE,
        purpose="Explain one server-owned contribution.",
        findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
        window_profile=_PROFILE,
        context=(),
    )
    key = InsightCacheKey(
        "analytical-scope-v1:fictional",
        "campaign-portfolio",
        "2026-09-29",
        "2026-10-05",
        "revision-fictional",
        "v1",
        InsightSlotId.CONTRIBUTION,
        PENDING_INPUT_DIGEST,
    )
    return InsightGenerationRequest(
        key=card_key(key, model_input),
        prompt=InsightPromptActivation(
            InsightSlotId.CONTRIBUTION, "v1", "A fictional prompt.", InsightModelRoute.QUICK
        ),
        next_step_options=options,
        **vars(model_input),
    )


def test_without_options_the_schema_has_no_next_step_property() -> None:
    envelope = card_envelope(_generation(()))
    assert "next_step" not in cast("dict[str, object]", envelope.schema["properties"])
    assert envelope.schema_id == "insight-narration-v1"


def test_with_options_the_schema_offers_their_ids_and_the_reason_bound() -> None:
    envelope = card_envelope(_generation(_OPTIONS))
    step = cast("dict[str, Any]", envelope.schema["properties"])["next_step"]
    assert step["properties"]["step_id"]["enum"] == ["s1", "s2"]
    assert step["properties"]["reason"] == {"type": "string", "minLength": 1, "maxLength": 240}
    assert envelope.schema_id == CARD_NEXT_STEP_SCHEMA_ID


class _Client:
    def __init__(self, tool_input: dict[str, object]) -> None:
        self.tool_input = tool_input

    def converse(self, **_: object) -> dict[str, object]:
        return {
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1, "outputTokens": 1},
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"name": "insight_narration_v1", "input": self.tool_input}}
                    ]
                }
            },
        }


def _decode(tool_input: dict[str, object], options: tuple[InsightNextStepOption, ...]) -> object:
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("Any", _Client(tool_input)),
    )
    return generator.generate(_generation(options))


def test_the_decoder_resolves_a_served_step_and_refuses_an_unserved_one() -> None:
    narration = cast(
        "InsightGeneratedNarration",
        _decode(
            {
                "output_class": "perspective",
                "sentences": ["A fictional reading."],
                "next_step": {"step_id": "s1", "reason": "It shows whether the drop persists."},
            },
            _OPTIONS,
        ),
    )
    assert narration.next_step is not None
    assert narration.next_step.target == _PRESET
    for step in ({"step_id": "s9", "reason": "A reason."}, {"step_id": "s1", "reason": ""}):
        with pytest.raises(InvariantError):
            _decode(
                {"output_class": "perspective", "sentences": ["A reading."], "next_step": step},
                _OPTIONS,
            )
    # Without served options, a step in the output is an output-contract break.
    with pytest.raises(InvariantError):
        _decode(
            {
                "output_class": "perspective",
                "sentences": ["A reading."],
                "next_step": {"step_id": "s1", "reason": "A reason."},
            },
            (),
        )
