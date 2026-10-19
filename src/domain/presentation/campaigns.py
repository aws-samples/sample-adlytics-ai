# SPDX-License-Identifier: MIT-0
"""Safe campaign page models and effective-window naming policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from domain.semantics.analytical_resolution import EffectiveLabel, EntityAlias, EntityKind
from domain.semantics.identity import CampaignKey, ChannelKey, MarketplaceKey, PlatformKey
from domain.shared_kernel.values import DateRange

__all__ = [
    "CampaignDetailPage",
    "CampaignListPage",
    "CampaignName",
    "CampaignPresentation",
    "CampaignPresentationRequest",
    "EffectiveNameStatus",
    "present_campaign",
]


class EffectiveNameStatus(StrEnum):
    """Why the selected effective name is present or absent."""

    AVAILABLE = "available"
    UNAVAILABLE_FOR_WINDOW = "unavailable_for_window"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class CampaignName:
    """One untrusted effective-dated display label."""

    value: str
    valid_from: date
    valid_to: date | None

    @classmethod
    def from_label(cls, label: EffectiveLabel) -> CampaignName:
        """Copy a governed alias label without changing its half-open interval."""
        return cls(label.value, label.valid_from, label.valid_to)


@dataclass(frozen=True, slots=True)
class CampaignPresentation:
    """Canonical campaign identity with safe display attributes only."""

    campaign: CampaignKey
    platform: PlatformKey
    channel: ChannelKey | None
    marketplace: MarketplaceKey | None
    effective_name: str | None
    effective_name_status: EffectiveNameStatus
    names: tuple[CampaignName, ...]

    def __post_init__(self) -> None:
        if self.campaign.platform != self.platform:
            raise ValueError("campaign presentation platform contradicts its canonical key.")
        if self.effective_name_status is EffectiveNameStatus.AVAILABLE and not self.effective_name:
            raise ValueError("an available campaign name requires display text.")
        if self.effective_name_status is EffectiveNameStatus.MISSING and (
            self.names or self.effective_name is not None
        ):
            raise ValueError("a campaign with no labels cannot fall back to canonical identity.")
        if self.effective_name_status is EffectiveNameStatus.UNAVAILABLE_FOR_WINDOW and (
            self.effective_name is not None or not self.names
        ):
            raise ValueError("an unavailable effective name retains history without fallback.")


@dataclass(frozen=True, slots=True)
class CampaignListPage:
    """Bounded campaign browse page with its exact authorized population count."""

    campaigns: tuple[CampaignPresentation, ...]
    visible_window: DateRange
    comparison_window: DateRange | None
    truncated: bool
    authorized_count: int

    def __post_init__(self) -> None:
        if self.authorized_count < len(self.campaigns):
            raise ValueError("campaign list count is smaller than its returned candidates.")
        if self.truncated != (self.authorized_count > len(self.campaigns)):
            raise ValueError("campaign list truncation contradicts its authorized count.")


@dataclass(frozen=True, slots=True)
class CampaignDetailPage:
    """One exact campaign detail page without numerical measurements."""

    campaign: CampaignPresentation
    visible_window: DateRange
    comparison_window: DateRange | None


@dataclass(frozen=True, slots=True)
class CampaignPresentationRequest:
    """Alias history and selected visible window used to derive one campaign card."""

    aliases: tuple[EntityAlias, ...]
    visible_window: DateRange

    def __post_init__(self) -> None:
        if not self.aliases:
            raise ValueError("campaign presentation requires an authorized alias history.")
        keys = {(alias.kind, alias.key) for alias in self.aliases}
        if len(keys) != 1 or next(iter(keys))[0] is not EntityKind.CAMPAIGN:
            raise ValueError("campaign presentation requires one canonical campaign history.")


def present_campaign(request: CampaignPresentationRequest) -> CampaignPresentation:
    """Apply the period-end name policy and preserve the complete authorized history."""
    aliases = request.aliases
    campaign = aliases[0].key
    if not isinstance(campaign, CampaignKey):  # pragma: no cover - request invariant
        raise TypeError("campaign alias history carries a non-campaign key.")
    labels = tuple(
        sorted(
            {label for alias in aliases for label in alias.labels},
            key=lambda item: (item.valid_from, item.valid_to or date.max, item.value),
        )
    )
    active = tuple({label.value for label in labels if label.contains(request.visible_window.end)})
    if len(active) > 1:
        raise ValueError("campaign has overlapping effective names at the visible-window end.")
    if active:
        effective_name = active[0]
        status = EffectiveNameStatus.AVAILABLE
    elif labels:
        effective_name = None
        status = EffectiveNameStatus.UNAVAILABLE_FOR_WINDOW
    else:
        effective_name = None
        status = EffectiveNameStatus.MISSING

    effective_aliases = tuple(
        alias
        for alias in aliases
        if alias.valid_from <= request.visible_window.end
        and (alias.valid_to is None or request.visible_window.start < alias.valid_to)
    )
    channels = {alias.channel for alias in effective_aliases if alias.channel is not None}
    marketplaces = {
        alias.marketplace for alias in effective_aliases if alias.marketplace is not None
    }
    if len(channels) > 1 or len(marketplaces) > 1:
        raise ValueError("campaign history has conflicting effective presentation context.")
    return CampaignPresentation(
        campaign=campaign,
        platform=campaign.platform,
        channel=next(iter(channels), None),
        marketplace=next(iter(marketplaces), None),
        effective_name=effective_name,
        effective_name_status=status,
        names=tuple(CampaignName.from_label(label) for label in labels),
    )
