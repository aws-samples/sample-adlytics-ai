# SPDX-License-Identifier: MIT-0
"""``NextStep.v2`` targets: one closed shape per kind, checked against navigation (ADR-122).

A card's next step names where it goes as a typed object, never a bare string, so a step can
say "open this tab at this preset" or "open this campaign". Three rules live here:

- **Declared targets only** (decision 5). Every target is checked against the routed tabs, and
  the target page's declared presets, filters and sections, when the options are built. The
  routed tabs are the v2 page entries with a ``navigation`` block, which is how
  ``presentation/navigation.yaml`` lists presentation tabs. An undeclared target raises
  :class:`NextStepTargetError`, a declaration failure, never a runtime drop.
- **Cached steps hold keys** (decision 4). A cached campaign step stores the server-only
  ``campaign_key``; only the serve path turns it into the ``campaign_ref`` capability it
  minted for this response, or drops the step. The wire codec refuses a key, and the cache
  codec refuses a capability, so neither can cross over.
- **Server-written questions** (decision 2). An ``ask_assistant`` question is dated with the
  window label, and one that states a cross-channel attributed total, or uses a causal verb
  about a full-funnel or frequency figure, is refused, so its step is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from domain.presentation.page_schemas import PresentationPageSchemaSet
from domain.presentation.question_rules import question_refusal
from domain.semantics.identity import CampaignKey

__all__ = [
    "MAX_ASSISTANT_QUESTION_CHARS",
    "AssistantTarget",
    "CampaignKeyTarget",
    "CampaignRefTarget",
    "MetricFocusTarget",
    "NextStepPageTargets",
    "NextStepTarget",
    "NextStepTargetError",
    "NextStepTargets",
    "PageTarget",
    "PresetTarget",
    "ProductRefTarget",
    "assistant_question_refusal",
    "campaign_step_key",
    "next_step_target_from_payload",
    "next_step_target_payload",
]

MAX_ASSISTANT_QUESTION_CHARS: Final = 240
"""ADR-122 decision 1: a question is 1 to 240 characters, the reason's bound."""
_GOVERNED_ID: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class NextStepTargetError(ValueError):
    """A step names a page, preset, filter, section or channel that nothing declares."""


def _text(value: str, field: str) -> str:
    if not isinstance(cast("object", value), str) or not value.strip():
        raise ValueError(f"a next-step target requires a nonblank {field}.")
    return value


def _governed(value: str | None, field: str) -> None:
    if value is not None and (
        not isinstance(cast("object", value), str) or not _GOVERNED_ID.fullmatch(value)
    ):
        raise ValueError(f"a next-step {field} must be a governed id.")


@dataclass(frozen=True, slots=True)
class PageTarget:
    """``open_page``: a tab, optionally at a preset, filter or section, or highlighting a channel.

    ``focus_campaign_ref`` waits for the forecasting amendment (ADR-122 consequences), so no
    server step carries it yet.
    """

    page_id: str
    preset_id: str | None = None
    filter_id: str | None = None
    section_id: str | None = None
    highlight_channel: str | None = None

    def __post_init__(self) -> None:
        _text(self.page_id, "page_id")
        for field in ("preset_id", "filter_id", "section_id", "highlight_channel"):
            _governed(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class PresetTarget:
    """``open_preset``: the current tab at another declared preset."""

    page_id: str
    preset_id: str

    def __post_init__(self) -> None:
        _text(self.page_id, "page_id")
        _governed(_text(self.preset_id, "preset_id"), "preset_id")


def campaign_step_key(campaign: CampaignKey) -> str:
    """Return the cached form of a campaign identity: scope, platform and key, never shown."""
    return "|".join((campaign.commerce_scope.value, campaign.platform.value, campaign.value))


@dataclass(frozen=True, slots=True)
class CampaignKeyTarget:
    """``open_campaign_detail`` as cached: the server-only campaign key, never sent."""

    page_id: str
    campaign_key: str

    def __post_init__(self) -> None:
        _text(self.page_id, "page_id")
        _text(self.campaign_key, "campaign_key")


@dataclass(frozen=True, slots=True)
class CampaignRefTarget:
    """``open_campaign_detail`` as served: the capability this response minted for the row."""

    page_id: str
    campaign_ref: str

    def __post_init__(self) -> None:
        _text(self.page_id, "page_id")
        _text(self.campaign_ref, "campaign_ref")


@dataclass(frozen=True, slots=True)
class ProductRefTarget:
    """``open_product_detail`` (ADR-125 decision 5): the row capability this response minted.

    Only the serve path writes it, from the parent custody's movers product, so it is never
    cached and has no key form. It opens detail in place on the page that served it, so it
    names no page.
    """

    selection_reference: str

    def __post_init__(self) -> None:
        _text(self.selection_reference, "selection_reference")


@dataclass(frozen=True, slots=True)
class MetricFocusTarget:
    """``open_metric_focus``: one metric's focus view on a tab, optionally under a filter."""

    page_id: str
    metric: str
    filter_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.page_id, "page_id")
        _text(self.metric, "metric")
        _governed(self.filter_id, "filter_id")


@dataclass(frozen=True, slots=True)
class AssistantTarget:
    """``ask_assistant``: the server-written, dated question the composer receives verbatim."""

    question: str

    def __post_init__(self) -> None:
        if (
            not _text(self.question, "question").strip()
            or len(self.question) > MAX_ASSISTANT_QUESTION_CHARS
        ):
            raise ValueError(
                f"an assistant question must be 1 to {MAX_ASSISTANT_QUESTION_CHARS} characters."
            )


type NextStepTarget = (
    PageTarget
    | PresetTarget
    | CampaignKeyTarget
    | CampaignRefTarget
    | ProductRefTarget
    | MetricFocusTarget
    | AssistantTarget
)


@dataclass(frozen=True, slots=True)
class NextStepPageTargets:
    """What one routed tab declares that a step may name."""

    page_id: str
    presets: frozenset[str]
    filters: frozenset[str]
    sections: frozenset[str]


@dataclass(frozen=True, slots=True)
class NextStepTargets:
    """The declared targets: routed tabs and their presets, filters and sections."""

    pages: Mapping[str, NextStepPageTargets]
    channels: frozenset[str] = frozenset()
    """Governed channel keys ``highlight_channel`` may name."""

    @classmethod
    def from_page_schemas(
        cls,
        schemas: PresentationPageSchemaSet,
        *,
        channels: frozenset[str] = frozenset(),
        filters: Mapping[str, frozenset[str]] | None = None,
    ) -> NextStepTargets:
        """Read the routed tabs from the v2 declaration; a page without navigation isn't one.

        ``filters`` names each page's declared filter ids: Campaigns' channel filter is All
        channels, Sponsored ads and each governed channel (ADR-127). A step whose filter a
        workspace doesn't offer is still dropped at serve time (ADR-122 decision 5). A vintage
        page (ADR-100 point 8) declares no presets, so no step may name one on it.
        """
        declared = filters or {}
        return cls(
            {
                page.page_id: NextStepPageTargets(
                    page_id=page.page_id,
                    presets=(
                        frozenset()
                        if page.vintage_policy is not None
                        else frozenset(
                            preset.value for preset in page.temporal_policy.candidate_presets
                        )
                    ),
                    filters=declared.get(page.page_id, frozenset()),
                    sections=frozenset(section.section_id for section in page.sections),
                )
                for page in schemas.pages
                if page.navigation is not None
            },
            channels,
        )

    def page(self, page_id: str) -> NextStepPageTargets:
        """Return a routed tab's declaration, or refuse an undeclared page."""
        declared = self.pages.get(page_id)
        if declared is None:
            raise NextStepTargetError(f"next-step page {page_id!r} is not a routed tab.")
        return declared

    def check(self, target: NextStepTarget | None) -> None:
        """Refuse a target that names anything the navigation or its page doesn't declare."""
        if target is None or isinstance(target, (AssistantTarget, ProductRefTarget)):
            return
        page = self.page(target.page_id)
        preset = getattr(target, "preset_id", None)
        if preset is not None and preset not in page.presets:
            raise NextStepTargetError(f"{page.page_id} declares no preset {preset!r}.")
        filter_id = getattr(target, "filter_id", None)
        if filter_id is not None and filter_id not in page.filters:
            raise NextStepTargetError(f"{page.page_id} declares no filter {filter_id!r}.")
        if isinstance(target, PageTarget):
            if target.section_id is not None and target.section_id not in page.sections:
                raise NextStepTargetError(
                    f"{page.page_id} declares no section {target.section_id!r}."
                )
            if target.highlight_channel is not None and target.highlight_channel not in (
                self.channels
            ):
                raise NextStepTargetError(
                    f"{target.highlight_channel!r} is not a governed channel key."
                )


# Wire and cache codecs ------------------------------------------------------------------------

_OPTIONAL_PAGE_FIELDS: Final = ("preset_id", "filter_id", "section_id", "highlight_channel")


def next_step_target_payload(  # noqa: PLR0911
    target: NextStepTarget | None, *, cached: bool
) -> object:
    """Encode a target with its closed key set; optional keys are omitted, never null.

    ``cached`` selects the cache form (a campaign key) over the wire form (a capability);
    each form refuses the other's campaign target.
    """
    if target is None:
        return None
    if isinstance(target, CampaignKeyTarget):
        if not cached:
            raise ValueError("a campaign key is server-only and is never served.")
        return {"page_id": target.page_id, "campaign_key": target.campaign_key}
    if isinstance(target, CampaignRefTarget):
        if cached:
            raise ValueError("a campaign capability is minted per serve and is never cached.")
        return {"page_id": target.page_id, "campaign_ref": target.campaign_ref}
    if isinstance(target, ProductRefTarget):
        if cached:
            raise ValueError("a product capability is minted per serve and is never cached.")
        return {"selection_reference": target.selection_reference}
    if isinstance(target, AssistantTarget):
        return {"question": target.question}
    if isinstance(target, PresetTarget):
        return {"page_id": target.page_id, "preset_id": target.preset_id}
    if isinstance(target, MetricFocusTarget):
        return {
            "page_id": target.page_id,
            "metric": target.metric,
            **({} if target.filter_id is None else {"filter_id": target.filter_id}),
        }
    return {
        "page_id": target.page_id,
        **{
            field: value
            for field in _OPTIONAL_PAGE_FIELDS
            if (value := getattr(target, field)) is not None
        },
    }


def _fields(value: object, required: frozenset[str], optional: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("a next-step target must be an object.")
    item = cast("dict[str, object]", value)
    if not required <= frozenset(item) <= required | optional:
        raise ValueError("next-step target fields are not exact.")
    if any(not isinstance(field, str) for field in item.values()):
        raise ValueError("next-step target fields must be text.")
    return cast("dict[str, str]", item)


def next_step_target_from_payload(  # noqa: PLR0911
    kind: str, value: object, *, cached: bool
) -> NextStepTarget | None:
    """Strictly decode one kind's target; the form not selected by ``cached`` is refused."""
    if kind == "open_evidence_status":
        if value is not None:
            raise ValueError("this next-step kind takes no target.")
        return None
    if kind == "open_campaign_detail":
        reference = "campaign_key" if cached else "campaign_ref"
        item = _fields(value, frozenset({"page_id", reference}), frozenset())
        if cached:
            return CampaignKeyTarget(item["page_id"], item["campaign_key"])
        return CampaignRefTarget(item["page_id"], item["campaign_ref"])
    if kind == "open_product_detail":
        if cached:
            raise ValueError("a product step is written per serve and is never cached.")
        item = _fields(value, frozenset({"selection_reference"}), frozenset())
        return ProductRefTarget(item["selection_reference"])
    if kind == "ask_assistant":
        return AssistantTarget(_fields(value, frozenset({"question"}), frozenset())["question"])
    if kind == "open_preset":
        item = _fields(value, frozenset({"page_id", "preset_id"}), frozenset())
        return PresetTarget(item["page_id"], item["preset_id"])
    if kind == "open_metric_focus":
        item = _fields(value, frozenset({"page_id", "metric"}), frozenset({"filter_id"}))
        return MetricFocusTarget(item["page_id"], item["metric"], item.get("filter_id"))
    if kind == "open_page":
        item = _fields(value, frozenset({"page_id"}), frozenset(_OPTIONAL_PAGE_FIELDS))
        return PageTarget(
            item["page_id"],
            item.get("preset_id"),
            item.get("filter_id"),
            item.get("section_id"),
            item.get("highlight_channel"),
        )
    raise ValueError(f"unknown next-step kind {kind!r}.")


# Questions (decision 2) -----------------------------------------------------------------------


def assistant_question_refusal(  # noqa: PLR0913
    question: str, *, window_label: str, cross_channel: bool, association_only: bool
) -> str | None:
    """Return why a written question can't be offered, or None when it can (§8.5 rules).

    ``cross_channel`` says the card reads figures from more than one governed channel, and
    ``association_only`` that its subject is a full-funnel (E1) or frequency (D2) figure.
    """
    return question_refusal(
        question,
        window_label=window_label,
        cross_channel=cross_channel,
        association_only=association_only,
        max_chars=MAX_ASSISTANT_QUESTION_CHARS,
    )
