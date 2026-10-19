# SPDX-License-Identifier: MIT-0
"""Campaign page naming, rename history, and missing-name fallback policy."""

from datetime import date

import pytest

from domain.presentation.campaigns import (
    CampaignPresentationRequest,
    EffectiveNameStatus,
    present_campaign,
)
from domain.semantics.analytical_resolution import (
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
)
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
CAMPAIGN = CampaignKey("campaign:fictional", SCOPE, PLATFORM)
CHANNEL = ChannelKey("amazon_dsp")
MARKETPLACE = MarketplaceKey("amazon_us")
WINDOW = DateRange(date(2026, 8, 10), date(2026, 8, 16))


def _alias(labels: tuple[EffectiveLabel, ...]) -> EntityAlias:
    return EntityAlias(
        kind=EntityKind.CAMPAIGN,
        key=CAMPAIGN,
        commerce_scope=SCOPE,
        labels=labels,
        source_identifiers=(SourceIdentifier("fictional.campaign", "source-1"),),
        evidence=MappingEvidence("fictional-campaign-presentation", 1),
        valid_from=date(2026, 1, 1),
        platform=PLATFORM,
        channel=CHANNEL,
        marketplace=MARKETPLACE,
    )


def test_period_end_name_is_selected_and_complete_rename_history_is_preserved() -> None:
    presentation = present_campaign(
        CampaignPresentationRequest(
            (
                _alias(
                    (
                        EffectiveLabel(
                            "Fictional Prospecting",
                            date(2026, 1, 1),
                            date(2026, 8, 12),
                        ),
                    )
                ),
                _alias((EffectiveLabel("Fictional Growth", date(2026, 8, 12)),)),
            ),
            WINDOW,
        )
    )

    assert presentation.effective_name_status is EffectiveNameStatus.AVAILABLE
    assert presentation.effective_name == "Fictional Growth"
    assert tuple(name.value for name in presentation.names) == (
        "Fictional Prospecting",
        "Fictional Growth",
    )
    assert presentation.channel == CHANNEL
    assert presentation.marketplace == MARKETPLACE


def test_missing_name_does_not_fall_back_to_canonical_identity() -> None:
    missing = present_campaign(CampaignPresentationRequest((_alias(()),), WINDOW))
    unavailable = present_campaign(
        CampaignPresentationRequest(
            (
                _alias(
                    (
                        EffectiveLabel(
                            "Historical Name",
                            date(2026, 1, 1),
                            date(2026, 2, 1),
                        ),
                    )
                ),
            ),
            WINDOW,
        )
    )

    assert missing.effective_name_status is EffectiveNameStatus.MISSING
    assert missing.effective_name is None
    assert missing.names == ()
    assert unavailable.effective_name_status is EffectiveNameStatus.UNAVAILABLE_FOR_WINDOW
    assert unavailable.effective_name is None
    assert tuple(name.value for name in unavailable.names) == ("Historical Name",)


def test_overlapping_effective_names_are_refused_instead_of_arbitrarily_selected() -> None:
    with pytest.raises(ValueError, match="overlapping effective names"):
        present_campaign(
            CampaignPresentationRequest(
                (
                    _alias(
                        (
                            EffectiveLabel("Name One", date(2026, 1, 1)),
                            EffectiveLabel("Name Two", date(2026, 1, 1)),
                        )
                    ),
                ),
                WINDOW,
            )
        )
