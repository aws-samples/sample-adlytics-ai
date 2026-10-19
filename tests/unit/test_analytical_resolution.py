# SPDX-License-Identifier: MIT-0
"""Authorized deterministic entity resolution and provenance records."""

from dataclasses import dataclass
from datetime import date

import pytest

from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityContext,
    EntityKind,
    EntityReference,
    FieldOrigin,
    MappingEvidence,
    ResolutionOutcome,
    ResolveEntityRequest,
    normalize_exact_text,
    resolve_entity,
)
from domain.semantics.identity import (
    BrandKey,
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-053", "ADR-054")]

SCOPE = CommerceScopeKey("amazon_retail_us")
OTHER_SCOPE = CommerceScopeKey("other_scope")
PLATFORM = PlatformKey("amazon_ads")
DSP = ChannelKey("amazon_dsp")
SPONSORED = ChannelKey("amazon_sponsored_brands")
MARKETPLACE = MarketplaceKey("amazon_us")
SOURCE_SYSTEM = SourceSystemKey("amc")
SOURCE_SCOPE = SourceIdentifier("amc.entity", "fictional-fixture-amc-entity")


DEFAULT_WINDOW = DateRange(date(2026, 8, 10), date(2026, 8, 14))
DEFAULT_CONTEXT = EntityContext()


@dataclass(frozen=True, slots=True)
class _CampaignOptions:
    channel: ChannelKey = DSP
    valid_from: date = date(2026, 1, 1)
    valid_to: date | None = None
    scope: CommerceScopeKey = SCOPE
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RequestOptions:
    kinds: frozenset[EntityKind] = frozenset({EntityKind.CAMPAIGN})
    window: DateRange = DEFAULT_WINDOW
    context: EntityContext = DEFAULT_CONTEXT


def _campaign(
    key: str,
    label: str | None,
    options: _CampaignOptions | None = None,
) -> EntityAlias:
    selected = options or _CampaignOptions()
    campaign = CampaignKey(key, selected.scope, PLATFORM)
    return EntityAlias(
        kind=EntityKind.CAMPAIGN,
        key=campaign,
        commerce_scope=selected.scope,
        labels=(EffectiveLabel(label, selected.valid_from, selected.valid_to),) if label else (),
        source_identifiers=(
            SourceIdentifier(
                "amc.campaign_id_string",
                selected.source_id or key.replace("campaign:", "A0"),
            ),
        ),
        evidence=MappingEvidence("synthetic-campaign-catalog", 1),
        valid_from=selected.valid_from,
        valid_to=selected.valid_to,
        source_system=SOURCE_SYSTEM,
        source_scope=SOURCE_SCOPE,
        platform=PLATFORM,
        channel=selected.channel,
        marketplace=MARKETPLACE,
    )


def _request(
    aliases: tuple[EntityAlias, ...],
    reference: EntityReference,
    options: _RequestOptions | None = None,
) -> ResolveEntityRequest:
    selected = options or _RequestOptions()
    return ResolveEntityRequest(
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        reference=reference,
        origin=FieldOrigin.EXPLICIT_CUSTOMER_TEXT,
        aliases=aliases,
        kinds=selected.kinds,
        window=selected.window,
        context=selected.context,
    )


def test_normalization_is_exact_after_case_punctuation_and_space_only() -> None:
    assert normalize_exact_text(" Northstar—HOME | Brand ") == "northstar home brand"
    assert normalize_exact_text("Northstar Home Brands") != normalize_exact_text(
        "Northstar Home Brand"
    )


def test_duplicate_authorized_campaign_names_require_clarification() -> None:
    aliases = (
        _campaign("campaign:dsp", "Northstar Home | Brand", _CampaignOptions(channel=DSP)),
        _campaign(
            "campaign:sponsored",
            "Northstar Home | Brand",
            _CampaignOptions(channel=SPONSORED),
        ),
    )

    record = resolve_entity(_request(aliases, EntityReference(text="northstar home brand")))

    assert record.outcome is ResolutionOutcome.AMBIGUOUS
    assert len(record.candidates) == 2
    assert {candidate.channel for candidate in record.candidates} == {DSP, SPONSORED}


def test_channel_context_deterministically_resolves_duplicate_name() -> None:
    aliases = (
        _campaign("campaign:dsp", "Northstar Home | Brand", _CampaignOptions(channel=DSP)),
        _campaign(
            "campaign:sponsored",
            "Northstar Home | Brand",
            _CampaignOptions(channel=SPONSORED),
        ),
    )

    record = resolve_entity(
        _request(
            aliases,
            EntityReference(text="Northstar Home Brand"),
            _RequestOptions(context=EntityContext(channel=DSP)),
        )
    )

    assert record.outcome is ResolutionOutcome.RESOLVED
    assert str(record.value) == "campaign:dsp"
    assert record.origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    assert record.evidence


def test_rename_inside_window_is_one_candidate_with_both_effective_labels() -> None:
    aliases = (
        _campaign(
            "campaign:stable",
            "Northstar Home | Prospecting",
            _CampaignOptions(valid_to=date(2026, 8, 12)),
        ),
        _campaign(
            "campaign:stable",
            "Northstar Home | Growth",
            _CampaignOptions(valid_from=date(2026, 8, 12)),
        ),
    )

    old = resolve_entity(_request(aliases, EntityReference(text="Northstar Home Prospecting")))
    new = resolve_entity(_request(aliases, EntityReference(text="Northstar Home Growth")))

    assert old.outcome is ResolutionOutcome.RESOLVED
    assert new.outcome is ResolutionOutcome.RESOLVED
    assert old.value == new.value


def test_reused_name_outside_window_does_not_create_ambiguity() -> None:
    aliases = (
        _campaign(
            "campaign:old",
            "Seasonal Brand",
            _CampaignOptions(
                valid_from=date(2025, 1, 1),
                valid_to=date(2025, 12, 31),
            ),
        ),
        _campaign(
            "campaign:current",
            "Seasonal Brand",
            _CampaignOptions(valid_from=date(2026, 1, 1)),
        ),
    )

    record = resolve_entity(_request(aliases, EntityReference(text="Seasonal Brand")))

    assert record.outcome is ResolutionOutcome.RESOLVED
    assert str(record.value) == "campaign:current"


def test_missing_name_resolves_only_through_fully_qualified_source_alias() -> None:
    alias = _campaign(
        "campaign:unnamed",
        None,
        _CampaignOptions(source_id="A0SYNTHETIC00000002"),
    )

    record = resolve_entity(
        _request(
            (alias,),
            EntityReference(
                source_identifier=SourceIdentifier("amc.campaign_id_string", "A0SYNTHETIC00000002"),
                source_system=SOURCE_SYSTEM,
                source_scope=SOURCE_SCOPE,
            ),
        )
    )

    assert record.outcome is ResolutionOutcome.RESOLVED
    assert str(record.value) == "campaign:unnamed"


def test_hidden_direct_key_and_unknown_key_disclose_no_candidates() -> None:
    hidden = _campaign(
        "campaign:hidden",
        "Hidden",
        _CampaignOptions(scope=OTHER_SCOPE),
    )
    hidden_key = CampaignKey("campaign:hidden", OTHER_SCOPE, PLATFORM)
    hidden_record = resolve_entity(_request((hidden,), EntityReference(canonical_key=hidden_key)))
    missing_record = resolve_entity(
        _request(
            (hidden,),
            EntityReference(canonical_key=CampaignKey("campaign:missing", SCOPE, PLATFORM)),
        )
    )

    assert hidden_record.outcome is ResolutionOutcome.UNAUTHORIZED_OR_HIDDEN
    assert missing_record.outcome is ResolutionOutcome.NOT_FOUND
    assert hidden_record.candidates == missing_record.candidates == ()


def test_campaign_and_brand_phrase_is_typed_entity_kind_ambiguity() -> None:
    campaign = _campaign("campaign:brand", "Northstar")
    brand = EntityAlias(
        kind=EntityKind.BRAND,
        key=BrandKey("brand:northstar", SCOPE),
        commerce_scope=SCOPE,
        labels=(EffectiveLabel("Northstar", date(2020, 1, 1)),),
        source_identifiers=(SourceIdentifier("vendor.brand_code", "NORTHSTAR"),),
        evidence=MappingEvidence("vendor-product-hierarchy", 1),
        valid_from=date(2020, 1, 1),
        marketplace=MARKETPLACE,
    )

    record = resolve_entity(
        _request(
            (campaign, brand),
            EntityReference(text="Northstar"),
            _RequestOptions(kinds=frozenset({EntityKind.CAMPAIGN, EntityKind.BRAND})),
        )
    )

    assert record.outcome is ResolutionOutcome.AMBIGUOUS
    assert {candidate.kind for candidate in record.candidates} == {
        EntityKind.CAMPAIGN,
        EntityKind.BRAND,
    }
