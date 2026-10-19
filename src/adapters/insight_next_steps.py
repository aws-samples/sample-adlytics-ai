# SPDX-License-Identifier: MIT-0
"""Card next steps and the FR-11 context a step needs (design 4.12; ADR-076 next-step marker).

Two parts with different switches:
- The FR-11 context entries (comparison baseline, data sufficiency, retail all-products
  drivers) ship active: they are context, so Gate-1 never filters them and the card input
  digest covers them.
- The served options and the ``available next steps`` entry exist only for a contract whose
  ``next_steps.allowed_kinds`` is non-empty. Every contract ships it empty until the gated
  card-next-step prompt does (R6-6), so no request carries options today.
- No derivation builds a :class:`NextStepSurface` yet: the surface producer ships with that
  prompt. Until a slot is in :data:`SURFACED_SLOTS`, a contract that lists a kind for it fails
  loading, so turning a kind on early is a declaration failure, never a per-card raise.

The model chooses a step by id from options the server built from views the page serves, so
"never recommend a view the product does not serve" is structural, not a text check
(ADR-082).

Options are ``NextStep.v2`` (ADR-122): each target is a typed object checked against the
routed tabs and the target page's declared presets, filters and sections as it is built, so a
step to an undeclared target fails here, never at serve. A campaign option holds the
campaign's key; the serve path maps it to that response's capability or drops it.
``open_page`` steps are declared by the page that serves them, never offered to a card's model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from domain.presentation.insights import (
    InsightContextEntry,
    InsightNextStepKind,
    InsightNextStepOption,
    InsightSlotId,
)
from domain.presentation.next_steps import (
    AssistantTarget,
    CampaignKeyTarget,
    MetricFocusTarget,
    NextStepTarget,
    NextStepTargets,
    PresetTarget,
    assistant_question_refusal,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.shared_kernel.values import DateRange

__all__ = [
    "NEXT_STEP_KIND_QUOTAS",
    "PRESET_STEP_LABELS",
    "SURFACED_SLOTS",
    "NextStepCampaign",
    "NextStepSurface",
    "available_next_steps_entry",
    "comparison_window_entry",
    "data_sufficiency_entry",
    "next_step_options",
]

PRESET_STEP_LABELS: Final[dict[TemporalPresetId, str]] = {
    TemporalPresetId.ONE_DAY: "Show 1 day",
    TemporalPresetId.SEVEN_DAYS: "Show 7 days",
    TemporalPresetId.FOURTEEN_DAYS: "Show 14 days",
    TemporalPresetId.THIRTY_DAYS: "Show 30 days",
}
"""One customer label per target preset (R6-12), keyed by the served preset id."""
EVIDENCE_STEP_LABEL: Final = "See why: this page's coverage"
"""Named for what it opens, so it never repeats a figure's own "See why" (M5-12)."""
ASSISTANT_STEP_LABEL: Final = "Ask the assistant"
DATA_COMPLETE: Final = "This view's data is complete."
SURFACED_SLOTS: Final[frozenset[InsightSlotId]] = frozenset()
"""The slots whose derivation builds a served :class:`NextStepSurface`; none yet.

The contract loader refuses a contract that lists a next-step kind for any other slot.
"""


def campaign_step_label(display_name: str) -> str:
    """Label an ``open_campaign_detail`` step with the served display name (ADR-067)."""
    return f"Open {display_name}"


def metric_step_label(metric_label: str) -> str:
    """Label an ``open_metric_focus`` step with the metric's dictionary label."""
    return f"Open {metric_label} by day"


@dataclass(frozen=True, slots=True)
class _Quota:
    portfolio: int
    other: int


NEXT_STEP_KIND_QUOTAS: Final[dict[InsightNextStepKind, _Quota]] = {
    InsightNextStepKind.OPEN_PRESET: _Quota(1, 1),
    InsightNextStepKind.OPEN_CAMPAIGN_DETAIL: _Quota(2, 0),
    InsightNextStepKind.OPEN_METRIC_FOCUS: _Quota(1, 2),
    InsightNextStepKind.OPEN_EVIDENCE_STATUS: _Quota(1, 1),
    InsightNextStepKind.ASK_ASSISTANT: _Quota(1, 1),
}
"""Fixed per-kind quotas in list order (R6-4): at most 6 on a portfolio slot, 5 elsewhere."""


@dataclass(frozen=True, slots=True)
class NextStepCampaign:
    """A campaign the card names, by its served display name and server-only campaign key."""

    display_name: str
    campaign_key: str
    """Cached with the step; the serve path maps it to that response's capability (ADR-122)."""


@dataclass(frozen=True, slots=True)
class NextStepSurface:
    """What one card's page serves that a step could open (built by its derivation)."""

    targets: NextStepTargets
    """The routed tabs and their declared presets, filters and sections."""
    page_id: str
    """The tab the card is on: preset, campaign and metric steps open there."""
    portfolio: bool
    """Portfolio page slots may open campaign detail; every other surface may not."""
    current_preset: TemporalPresetId
    available_presets: tuple[TemporalPresetId, ...]
    """The presets the page serves as available, in the page's order."""
    campaigns: tuple[NextStepCampaign, ...]
    """Campaigns the card's findings or drivers name, in finding then driver order."""
    metrics: tuple[tuple[str, str], ...]
    """(metric id, label): the card's own metrics first, then the surface's focusable ones."""
    evidence_complete: bool
    assistant_question: str | None
    """Written by the derivation from the card's validated findings, with the window label."""
    window_label: str
    """The artifact's window label, fixed per revision, so a cached question stays true."""
    cross_channel: bool
    """The card reads figures from more than one governed channel."""
    association_only: bool
    """The card's subject is a full-funnel (E1) or frequency (D2) figure."""


_PRESET_ORDER: Final = (
    TemporalPresetId.ONE_DAY,
    TemporalPresetId.SEVEN_DAYS,
    TemporalPresetId.FOURTEEN_DAYS,
    TemporalPresetId.THIRTY_DAYS,
)


def _preset_target(surface: NextStepSurface) -> TemporalPresetId | None:
    """Return the next longer available preset, or from the longest the next shorter."""
    available = [preset for preset in _PRESET_ORDER if preset in surface.available_presets]
    if surface.current_preset not in available:
        return None
    index = available.index(surface.current_preset)
    if index + 1 < len(available):
        return available[index + 1]
    return available[index - 1] if index > 0 else None


def _assistant_targets(surface: NextStepSurface) -> list[tuple[NextStepTarget | None, str]]:
    """Offer the written question, or nothing when §8.5's text rules refuse it (decision 2)."""
    question = surface.assistant_question
    if question is None or assistant_question_refusal(
        question,
        window_label=surface.window_label,
        cross_channel=surface.cross_channel,
        association_only=surface.association_only,
    ):
        return []
    return [(AssistantTarget(question), ASSISTANT_STEP_LABEL)]


def _kind_targets(
    surface: NextStepSurface, kind: InsightNextStepKind
) -> list[tuple[NextStepTarget | None, str]]:
    page = surface.page_id
    if kind is InsightNextStepKind.OPEN_PRESET:
        target = _preset_target(surface)
        return (
            []
            if target is None
            else [(PresetTarget(page, target.value), PRESET_STEP_LABELS[target])]
        )
    if kind is InsightNextStepKind.OPEN_CAMPAIGN_DETAIL:
        return [
            (
                CampaignKeyTarget(page, campaign.campaign_key),
                campaign_step_label(campaign.display_name),
            )
            for campaign in dict.fromkeys(surface.campaigns)
        ]
    if kind is InsightNextStepKind.OPEN_METRIC_FOCUS:
        return [
            (MetricFocusTarget(page, metric_id), metric_step_label(label))
            for metric_id, label in dict.fromkeys(surface.metrics)
        ]
    if kind is InsightNextStepKind.OPEN_EVIDENCE_STATUS:
        return [] if surface.evidence_complete else [(None, EVIDENCE_STEP_LABEL)]
    return _assistant_targets(surface)


def next_step_options(
    surface: NextStepSurface | None, allowed_kinds: tuple[InsightNextStepKind, ...]
) -> tuple[InsightNextStepOption, ...]:
    """Build the served options for one card, in quota table order with per-request ids.

    A contract that allows no kind builds nothing. A contract that allows kinds needs the
    surface its derivation builds; asking without one is a wiring defect, so it is refused
    rather than answered with a guessed list. Every target is checked against the declared
    navigation first: an undeclared page, preset, filter or section raises
    ``NextStepTargetError`` (ADR-122 decision 5).
    """
    if not allowed_kinds:
        return ()
    if surface is None:
        raise ValueError("a card that allows next steps requires its served surface.")
    allowed = frozenset(allowed_kinds)
    found: list[tuple[InsightNextStepKind, NextStepTarget | None, str]] = []
    for kind, quota in NEXT_STEP_KIND_QUOTAS.items():
        if kind not in allowed:
            continue
        limit = quota.portfolio if surface.portfolio else quota.other
        for target, label in _kind_targets(surface, kind)[:limit]:
            surface.targets.check(target)
            found.append((kind, target, label))
    return tuple(
        InsightNextStepOption(f"s{index}", kind, target, label)
        for index, (kind, target, label) in enumerate(found, start=1)
    )


def available_next_steps_entry(
    options: tuple[InsightNextStepOption, ...],
) -> InsightContextEntry | None:
    """Return the ``available next steps`` entry, or None without options."""
    if not options:
        return None
    return InsightContextEntry(
        "available next steps",
        "; ".join(f"{option.step_id}: {option.label}" for option in options),
    )


def comparison_window_entry(prior: DateRange) -> InsightContextEntry:
    """Return the comparison baseline in customer words (FR-11; one helper)."""
    return InsightContextEntry(
        "comparison window",
        f"{prior.start} to {prior.end}, the {prior.days} days before this view",
    )


def data_sufficiency_entry(
    qualifiers: Iterable[str], coverage_holes: Iterable[DateRange] = ()
) -> InsightContextEntry:
    """Say whether this view's data is complete, naming the gaps the page already serves."""
    gaps = sorted({value.strip() for value in qualifiers if value.strip()})
    gaps.extend(
        f"no data for {hole.start}" if hole.days == 1 else f"no data for {hole.start} to {hole.end}"
        for hole in sorted(set(coverage_holes), key=lambda item: item.start)
    )
    if not gaps:
        return InsightContextEntry("data sufficiency", DATA_COMPLETE)
    return InsightContextEntry(
        "data sufficiency", f"This view's data is not complete: {'; '.join(gaps)}."
    )
