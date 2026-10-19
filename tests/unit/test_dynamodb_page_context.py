# SPDX-License-Identifier: MIT-0
"""DynamoDB page-context request shapes and immutable-record behavior."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.page_context import (
    CAMPAIGN_CAPABILITY_PREFIX,
    CHANNEL_CAPABILITY_PREFIX,
    EXPIRES_AT,
    LOOKUP_KEY,
    MAX_DYNAMODB_ITEM_BYTES,
    MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS,
    DynamoPageContextStore,
    parent_capability_transaction_fits,
)
from domain.presentation.artifacts import (
    CampaignSelectionCapabilityReadRequest,
    CampaignSelectionCapabilityRecord,
    ChannelRowCapabilityReadRequest,
    ChannelRowCapabilityRecord,
    MediaScope,
    MediaScopeCapabilityReadRequest,
    MediaScopeCapabilityRecord,
    MediaWindowCapabilityReadRequest,
    MediaWindowCapabilityRecord,
    PageArtifact,
    PageArtifactRecord,
    ParentCapabilityIssuanceOutcome,
    ParentCapabilityIssuanceRequest,
    ProductSelectionCapabilityReadRequest,
    ProductSelectionCapabilityRecord,
    SafeMediaScopeDisplaySignature,
    VisualNarrationReferenceReadRequest,
    VisualNarrationReferenceRecord,
    canonical_artifact_json,
)
from domain.presentation.campaign_attribution import SafeCampaignDisplaySignature
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.retail_product_detail import SafeProductDisplaySignature
from domain.presentation.temporal import (
    TemporalComparison,
    TemporalComparisonPolicy,
    TemporalComparisonStatus,
    TemporalDisplayGrain,
    TemporalReportingWindowStatus,
    TemporalResolved,
    TemporalResolvedSelection,
    TemporalResolvedSelectionKind,
)
from domain.semantics.analytical_resolution import ResolutionOutcome
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
)
from domain.semantics.page_context import (
    CampaignSelectionReadRequest,
    PageContextDisplayLabels,
    PageContextLabels,
    PageContextRecord,
    PageContextRedemptionOutcome,
    PageContextRedemptionRequest,
    PageContextReference,
    PageContextSnapshot,
    ReportingWindowStatus,
)
from domain.shared_kernel.errors import DependencyError, InvariantError
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
EXPIRY = NOW + timedelta(hours=1)
REFERENCE = PageContextReference("A" * 43)
SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
RECORD = PageContextRecord(
    reference=REFERENCE,
    snapshot=PageContextSnapshot(
        response_identity="response-fictional",
        visible_window=DateRange(date(2026, 8, 10), date(2026, 8, 16)),
        reporting_window_status=ReportingWindowStatus.PREFERRED_DEFAULT,
        campaign=CampaignKey("campaign:fictional", SCOPE, PLATFORM),
        channel=ChannelKey("amazon_dsp"),
        marketplace=MarketplaceKey("amazon_us"),
    ),
    verified_owner_id="owner",
    conversation_session_id="session",
    expires_at=EXPIRY,
)
SELECTION_READ = CampaignSelectionReadRequest(REFERENCE, "owner", "session", NOW)


@guard_reserved_words
@dataclass
class RecordingDynamo:
    """Minimal recording client that preserves exact low-level request shapes."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    puts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    transactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    gets: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fail_get: bool = False
    fail_put: bool = False
    fail_transaction: bool = False

    def put_item(self, **request: Any) -> dict[str, Any]:
        self.puts.append(request)
        if self.fail_put:
            raise RuntimeError("write failed")
        item = cast("dict[str, dict[str, Any]]", request["Item"])
        key = str(item[LOOKUP_KEY]["S"])
        if key in self.items and "ConditionExpression" in request:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = item
        return {}

    def transact_write_items(self, **request: Any) -> dict[str, Any]:
        self.transactions.append(request)
        if self.fail_transaction:
            raise RuntimeError("transaction write failed")
        puts = [cast("dict[str, Any]", item["Put"]) for item in request["TransactItems"]]
        keys = [
            str(cast("dict[str, dict[str, str]]", put["Item"])[LOOKUP_KEY]["S"]) for put in puts
        ]
        if len(keys) != len(set(keys)) or any(key in self.items for key in keys):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "exists"},
                    "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                },
                "TransactWriteItems",
            )
        for key, put in zip(keys, puts, strict=True):
            self.items[key] = cast("dict[str, Any]", put["Item"])
        return {}

    def get_item(self, **request: Any) -> dict[str, Any]:
        self.gets.append(request)
        if self.fail_get:
            raise RuntimeError("read failed")
        key = str(cast("dict[str, dict[str, str]]", request["Key"])[LOOKUP_KEY]["S"])
        item = self.items.get(key)
        return {} if item is None else {"Item": item}


def _store(client: RecordingDynamo) -> DynamoPageContextStore:
    return DynamoPageContextStore("page-context-table", cast("Any", client))


def test_put_uses_one_lookup_key_numeric_ttl_and_immutable_condition() -> None:
    client = RecordingDynamo()

    _store(client).put(RECORD)

    assert len(client.puts) == 1
    request = client.puts[0]
    assert request["TableName"] == "page-context-table"
    assert request["ConditionExpression"] == "attribute_not_exists(lookup_key)"
    item = cast("dict[str, dict[str, str]]", request["Item"])
    assert item[LOOKUP_KEY] == {"S": f"page#{REFERENCE.value}"}
    assert item[EXPIRES_AT] == {"N": str(int(EXPIRY.timestamp()))}
    assert set(item) == {LOOKUP_KEY, EXPIRES_AT, "payload"}


def test_selection_uses_exact_namespace_strong_read_and_canonical_attestation() -> None:
    client = RecordingDynamo()
    store = _store(client)

    store.put_selection(RECORD)
    resolved = store.read_selection(SELECTION_READ)

    assert client.puts[0]["ConditionExpression"] == "attribute_not_exists(lookup_key)"
    assert client.items[f"selection#{REFERENCE.value}"][LOOKUP_KEY] == {
        "S": f"selection#{REFERENCE.value}"
    }
    assert client.gets[-1] == {
        "TableName": "page-context-table",
        "Key": {LOOKUP_KEY: {"S": f"selection#{REFERENCE.value}"}},
        "ConsistentRead": True,
    }
    assert resolved.outcome is ResolutionOutcome.RESOLVED
    assert resolved.selection is not None
    assert resolved.selection.snapshot == RECORD.snapshot

    client.items[f"selection#{REFERENCE.value}"]["unexpected"] = {"S": "value"}
    with pytest.raises(InvariantError, match="malformed"):
        store.read_selection(SELECTION_READ)


def test_exact_replay_is_accepted_and_conflicting_bytes_are_invariant() -> None:
    client = RecordingDynamo()
    store = _store(client)
    store.put(RECORD)

    store.put(RECORD)

    assert client.gets[-1]["ConsistentRead"] is True
    with pytest.raises(InvariantError, match="different immutable bytes"):
        store.put(replace(RECORD, verified_owner_id="other-owner"))


def test_campaign_capability_uses_distinct_namespace_and_strong_parent_bound_read() -> None:
    client = RecordingDynamo()
    store = _store(client)
    artifact = PageArtifact(
        "CampaignAttribution",
        1,
        canonical_artifact_json({"kind": "campaign_list", "outcome": "choices"}),
    )
    parent = PageArtifactRecord(
        REFERENCE,
        artifact,
        RECORD.verified_owner_id,
        RECORD.conversation_session_id,
        EXPIRY,
    )
    capability_reference = PageContextReference("B" * 43)
    capability = CampaignSelectionCapabilityRecord(
        reference=capability_reference,
        parent_artifact_reference=REFERENCE,
        parent_artifact_type=artifact.artifact_type,
        parent_artifact_version=artifact.artifact_version,
        verified_owner_id=parent.verified_owner_id,
        conversation_session_id=parent.conversation_session_id,
        expires_at=EXPIRY,
        campaign=cast("CampaignKey", RECORD.snapshot.campaign),
        channel=cast("ChannelKey", RECORD.snapshot.channel),
        marketplace=cast("MarketplaceKey", RECORD.snapshot.marketplace),
        safe_signature=SafeCampaignDisplaySignature(
            effective_name="Fictional campaign",
            channel="Fictional channel",
            marketplace="Fictional marketplace",
            platform="Fictional platform",
            effective_name_status=EffectiveNameStatus.AVAILABLE,
            effective_name_valid_from=date(2026, 1, 1),
        ),
        reporting_window_status=ReportingWindowStatus.PREFERRED_DEFAULT,
    )
    read = CampaignSelectionCapabilityReadRequest(
        capability_reference,
        parent.verified_owner_id,
        parent.conversation_session_id,
        NOW,
    )

    store.put_artifact(parent)
    store.put_campaign_capability(capability)
    resolved = store.read_campaign_capability(read)

    key = f"{CAMPAIGN_CAPABILITY_PREFIX}{capability_reference.value}"
    assert client.items[key][LOOKUP_KEY] == {"S": key}
    assert resolved.outcome is ResolutionOutcome.RESOLVED
    assert resolved.capability is not None
    assert resolved.capability.safe_signature == capability.safe_signature
    assert all(request["ConsistentRead"] is True for request in client.gets)
    assert (
        store.read_campaign_capability(replace(read, verified_owner_id="wrong-owner")).outcome
        is ResolutionOutcome.INVALID_CONTEXT
    )


def test_sdk_failures_are_translated_without_infrastructure_details() -> None:
    read_client = RecordingDynamo(fail_get=True)
    with pytest.raises(DependencyError) as read_error:
        _store(read_client).read_selection(SELECTION_READ)
    assert "page-context-table" not in read_error.value.user_visible_description

    write_client = RecordingDynamo(fail_put=True)
    with pytest.raises(DependencyError):
        _store(write_client).put(RECORD)


def _campaign_parent_capability() -> tuple[PageArtifactRecord, CampaignSelectionCapabilityRecord]:
    artifact = PageArtifact(
        "CampaignAttribution",
        1,
        canonical_artifact_json({"kind": "campaign_list", "outcome": "choices"}),
    )
    parent = PageArtifactRecord(
        REFERENCE,
        artifact,
        RECORD.verified_owner_id,
        RECORD.conversation_session_id,
        EXPIRY,
    )
    capability = CampaignSelectionCapabilityRecord(
        reference=PageContextReference("B" * 43),
        parent_artifact_reference=parent.reference,
        parent_artifact_type=artifact.artifact_type,
        parent_artifact_version=artifact.artifact_version,
        verified_owner_id=parent.verified_owner_id,
        conversation_session_id=parent.conversation_session_id,
        expires_at=EXPIRY,
        campaign=cast("CampaignKey", RECORD.snapshot.campaign),
        channel=cast("ChannelKey", RECORD.snapshot.channel),
        marketplace=cast("MarketplaceKey", RECORD.snapshot.marketplace),
        safe_signature=SafeCampaignDisplaySignature(
            effective_name="Fictional campaign",
            channel="Fictional channel",
            marketplace="Fictional marketplace",
            platform="Fictional platform",
            effective_name_status=EffectiveNameStatus.AVAILABLE,
            effective_name_valid_from=date(2026, 1, 1),
        ),
        reporting_window_status=ReportingWindowStatus.PREFERRED_DEFAULT,
    )
    return parent, capability


def _channel_parent_capabilities() -> tuple[
    PageArtifactRecord, tuple[ChannelRowCapabilityRecord, ...]
]:
    artifact = PageArtifact(
        "ChannelAttributionRobustness",
        2,
        canonical_artifact_json({"outcome": "ready"}),
    )
    parent = PageArtifactRecord(
        REFERENCE,
        artifact,
        RECORD.verified_owner_id,
        RECORD.conversation_session_id,
        EXPIRY,
    )
    capabilities = tuple(
        ChannelRowCapabilityRecord(
            reference=PageContextReference(f"{prefix}" * 43),
            parent_artifact_reference=parent.reference,
            parent_artifact_type=artifact.artifact_type,
            parent_artifact_version=artifact.artifact_version,
            verified_owner_id=parent.verified_owner_id,
            conversation_session_id=parent.conversation_session_id,
            expires_at=parent.expires_at,
            channel=ChannelKey(f"fictional-channel-{prefix.lower()}"),
            marketplace=cast("MarketplaceKey", RECORD.snapshot.marketplace),
            current_period=RECORD.snapshot.visible_window,
            model_ids=("first_touch_v1", "last_touch_v1", "linear_v1"),
            reporting_window_status=ReportingWindowStatus.PREFERRED_DEFAULT,
        )
        for prefix in ("C", "D")
    )
    return parent, capabilities


def test_channel_parent_and_rows_issue_in_one_immutable_transaction() -> None:
    client = RecordingDynamo()
    store = _store(client)
    parent, capabilities = _channel_parent_capabilities()

    result = store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, capabilities))

    assert result.outcome is ParentCapabilityIssuanceOutcome.ISSUED
    assert len(client.transactions) == 1
    assert len(client.transactions[0]["TransactItems"]) == 3
    assert set(client.items) == {
        f"artifact#{parent.reference.value}",
        *(f"{CHANNEL_CAPABILITY_PREFIX}{record.reference.value}" for record in capabilities),
    }
    assert all(
        store.read_channel_capability(
            ChannelRowCapabilityReadRequest(record.reference, "owner", "session", NOW)
        ).outcome
        is ResolutionOutcome.RESOLVED
        for record in capabilities
    )

    client = RecordingDynamo()
    store = _store(client)
    parent, capability = _campaign_parent_capability()

    result = store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, (capability,)))

    assert result.outcome is ParentCapabilityIssuanceOutcome.ISSUED
    assert len(client.transactions) == 1
    items = client.transactions[0]["TransactItems"]
    assert len(items) == 2
    assert all(
        item["Put"]["ConditionExpression"] == "attribute_not_exists(lookup_key)" for item in items
    )
    assert set(client.items) == {
        f"artifact#{parent.reference.value}",
        f"{CAMPAIGN_CAPABILITY_PREFIX}{capability.reference.value}",
    }
    assert (
        store.read_campaign_capability(
            CampaignSelectionCapabilityReadRequest(capability.reference, "owner", "session", NOW)
        ).outcome
        is ResolutionOutcome.RESOLVED
    )
    assert all(request["ConsistentRead"] is True for request in client.gets)


def test_parent_capability_issuance_refuses_over_capacity_without_writing_any_record() -> None:
    client = RecordingDynamo()
    store = _store(client)
    parent, capability = _campaign_parent_capability()
    capabilities = tuple(
        replace(capability, reference=PageContextReference(f"{index:043d}"))
        for index in range(MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS)
    )

    result = store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, capabilities))

    assert result.outcome is ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED
    assert client.transactions == []
    assert client.items == {}


def test_failed_parent_capability_transaction_leaves_no_parent_or_orphan_capability() -> None:
    client = RecordingDynamo(fail_transaction=True)
    store = _store(client)
    parent, capability = _campaign_parent_capability()

    with pytest.raises(DependencyError):
        store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, (capability,)))

    assert len(client.transactions) == 1
    assert client.items == {}


def test_parent_capability_issuance_accepts_only_exact_immutable_replay() -> None:
    client = RecordingDynamo()
    store = _store(client)
    parent, capability = _campaign_parent_capability()
    request = ParentCapabilityIssuanceRequest(parent, (capability,))

    store.issue_parent_capabilities(request)
    assert store.issue_parent_capabilities(request).issued is True
    with pytest.raises(InvariantError, match="different immutable bytes"):
        store.issue_parent_capabilities(
            ParentCapabilityIssuanceRequest(
                parent,
                (
                    replace(
                        capability,
                        safe_signature=replace(
                            capability.safe_signature, effective_name="Other campaign"
                        ),
                    ),
                ),
            )
        )


def test_capability_reference_cannot_be_redeemed_through_another_kind_namespace() -> None:
    client = RecordingDynamo()
    store = _store(client)
    parent, capability = _campaign_parent_capability()
    store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, (capability,)))

    wrong_kind = store.read_channel_capability(
        ChannelRowCapabilityReadRequest(capability.reference, "owner", "session", NOW)
    )

    assert wrong_kind.outcome is ResolutionOutcome.INVALID_CONTEXT
    assert client.gets[-1]["Key"] == {
        LOOKUP_KEY: {"S": f"channel-capability#{capability.reference.value}"}
    }


def test_new_capability_kinds_issue_atomically_redeem_and_reject_signature_drift() -> None:
    client = RecordingDynamo()
    store = _store(client)
    artifact = PageArtifact(
        "MediaEfficiencyScopeList",
        1,
        canonical_artifact_json({"outcome": "choices"}),
    )
    parent = PageArtifactRecord(
        REFERENCE,
        artifact,
        RECORD.verified_owner_id,
        RECORD.conversation_session_id,
        EXPIRY,
    )
    resolved = TemporalResolved(
        window=RECORD.snapshot.visible_window,
        reporting_window_status=TemporalReportingWindowStatus.USER_SELECTED,
        selection=TemporalResolvedSelection(
            kind=TemporalResolvedSelectionKind.PRESET,
            preset_id=TemporalPresetId.SEVEN_DAYS,
        ),
        display_grain=TemporalDisplayGrain.DAY,
    )
    comparison = TemporalComparison(
        policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
        status=TemporalComparisonStatus.AVAILABLE,
    )
    product = ProductSelectionCapabilityRecord(
        reference=PageContextReference("C" * 43),
        parent_artifact_reference=parent.reference,
        parent_artifact_type=artifact.artifact_type,
        parent_artifact_version=artifact.artifact_version,
        verified_owner_id=parent.verified_owner_id,
        conversation_session_id=parent.conversation_session_id,
        expires_at=EXPIRY,
        product=ProductKey("fictional-product", SCOPE, MarketplaceKey("amazon_us")),
        safe_signature=SafeProductDisplaySignature(
            effective_name="Fictional product",
            effective_name_status=EffectiveNameStatus.AVAILABLE,
            marketplace="Fictional marketplace",
        ),
        catalog_identity="fictional-catalog-v1",
    )
    scope = MediaScopeCapabilityRecord(
        reference=PageContextReference("D" * 43),
        parent_artifact_reference=parent.reference,
        parent_artifact_type=artifact.artifact_type,
        parent_artifact_version=artifact.artifact_version,
        verified_owner_id=parent.verified_owner_id,
        conversation_session_id=parent.conversation_session_id,
        expires_at=EXPIRY,
        scope=MediaScope(
            commerce_scope=SCOPE,
            platform=PLATFORM,
            channel=ChannelKey("amazon_dsp"),
            marketplace=MarketplaceKey("amazon_us"),
        ),
        safe_signature=SafeMediaScopeDisplaySignature("Fictional media scope"),
        catalog_identity="fictional-catalog-v1",
    )
    window = MediaWindowCapabilityRecord(
        reference=PageContextReference("D" * 42 + "E"),
        parent_artifact_reference=parent.reference,
        parent_artifact_type=artifact.artifact_type,
        parent_artifact_version=artifact.artifact_version,
        verified_owner_id=parent.verified_owner_id,
        conversation_session_id=parent.conversation_session_id,
        expires_at=EXPIRY,
        resolved=resolved,
        compare_previous=True,
        comparison=comparison,
    )
    narration = VisualNarrationReferenceRecord(
        reference=PageContextReference("D" * 42 + "F"),
        parent_artifact_reference=parent.reference,
        parent_artifact_type=artifact.artifact_type,
        parent_artifact_version=artifact.artifact_version,
        verified_owner_id=parent.verified_owner_id,
        conversation_session_id=parent.conversation_session_id,
        expires_at=EXPIRY,
        visual_id="delivery-trend",
        metric_cohort=("spend", "impressions"),
        safe_scope_label="Fictional media scope",
        resolved=resolved,
        basis=MeasurementBasis.AD_ATTRIBUTED,
        qualification=("qualified",),
        allowed_action_ids=("open-exact-data",),
    )
    capabilities = (product, scope, window, narration)

    result = store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, capabilities))

    expected_keys = {
        f"artifact#{parent.reference.value}",
        f"product-capability#{product.reference.value}",
        f"media-scope-capability#{scope.reference.value}",
        f"media-window-capability#{window.reference.value}",
        f"visual-narration-reference#{narration.reference.value}",
    }
    assert result.outcome is ParentCapabilityIssuanceOutcome.ISSUED
    assert set(client.items) == expected_keys
    assert len(client.transactions) == 1
    assert all(
        request["Put"]["ConditionExpression"] == "attribute_not_exists(lookup_key)"
        for request in client.transactions[0]["TransactItems"]
    )
    assert (
        store.read_product_capability(
            ProductSelectionCapabilityReadRequest(product.reference, "owner", "session", NOW)
        ).outcome
        is ResolutionOutcome.RESOLVED
    )
    assert (
        store.read_media_scope_capability(
            MediaScopeCapabilityReadRequest(scope.reference, "owner", "session", NOW)
        ).outcome
        is ResolutionOutcome.RESOLVED
    )
    assert (
        store.read_media_window_capability(
            MediaWindowCapabilityReadRequest(window.reference, "owner", "session", NOW)
        ).outcome
        is ResolutionOutcome.RESOLVED
    )
    assert (
        store.read_visual_narration_reference(
            VisualNarrationReferenceReadRequest(narration.reference, "owner", "session", NOW)
        ).outcome
        is ResolutionOutcome.RESOLVED
    )
    assert all(request["ConsistentRead"] is True for request in client.gets)

    with pytest.raises(InvariantError, match="different immutable bytes"):
        store.issue_parent_capabilities(
            ParentCapabilityIssuanceRequest(
                parent,
                (
                    replace(
                        product,
                        safe_signature=replace(
                            product.safe_signature,
                            effective_name="Different fictional product",
                        ),
                    ),
                    scope,
                    window,
                    narration,
                ),
            )
        )


def test_parent_capability_transaction_applies_explicit_item_and_encoded_byte_bounds() -> None:
    within = tuple(
        {"lookup_key": {"S": f"fictional-{index}"}}
        for index in range(MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS)
    )
    oversized = ({"payload": {"S": "x" * MAX_DYNAMODB_ITEM_BYTES}},)

    assert parent_capability_transaction_fits(cast("Any", within))
    assert not parent_capability_transaction_fits(cast("Any", oversized))


# ADR-120: the `page#` record a v2 parent serve writes before its transaction -----------------

LABELS = PageContextLabels(
    page_id="campaigns",
    preset_id="7d",
    page_context_label="Campaigns · Aug 10\u201316, 2026",
    display_labels=PageContextDisplayLabels(page="Campaigns", window="Aug 10\u201316, 2026"),
)
PAGE_RECORD = replace(RECORD, snapshot=replace(RECORD.snapshot, labels=LABELS))


def _redeem(store: DynamoPageContextStore, owner: str = "owner", session: str = "session"):
    return store.read_page_context(PageContextRedemptionRequest(REFERENCE, owner, session, NOW))


@pytest.mark.covers("ADR-120")
def test_a_full_50_row_segment_writes_its_page_item_and_still_fits_51_transaction_items() -> None:
    client = RecordingDynamo()
    store = _store(client)
    parent, capability = _campaign_parent_capability()
    capabilities = tuple(
        replace(capability, reference=PageContextReference(f"{index:043d}"))
        for index in range(MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS - 1)
    )

    store.put(PAGE_RECORD)
    result = store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, capabilities))

    assert result.issued is True
    assert len(capabilities) == 50
    # The page record is its own conditional put, before and outside the transaction.
    assert [put["Item"][LOOKUP_KEY]["S"] for put in client.puts] == [f"page#{REFERENCE.value}"]
    assert client.puts[0]["ConditionExpression"] == f"attribute_not_exists({LOOKUP_KEY})"
    (transaction,) = client.transactions
    assert len(transaction["TransactItems"]) == MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS == 51
    assert all(
        not item["Put"]["Item"][LOOKUP_KEY]["S"].startswith("page#")
        for item in transaction["TransactItems"]
    )
    assert f"page#{REFERENCE.value}" in client.items
    assert _redeem(store).outcome is PageContextRedemptionOutcome.RESOLVED


@pytest.mark.covers("ADR-120")
def test_a_forced_transaction_failure_after_the_page_put_leaves_only_an_orphan_page_item() -> None:
    client = RecordingDynamo(fail_transaction=True)
    store = _store(client)
    parent, capability = _campaign_parent_capability()

    store.put(PAGE_RECORD)
    with pytest.raises(DependencyError):
        store.issue_parent_capabilities(ParentCapabilityIssuanceRequest(parent, (capability,)))

    # No artifact and no capability: the orphan names no bytes and expires on its own TTL.
    assert list(client.items) == [f"page#{REFERENCE.value}"]
    assert client.items[f"page#{REFERENCE.value}"][EXPIRES_AT] == {
        "N": str(int(EXPIRY.timestamp()))
    }


@pytest.mark.covers("ADR-120")
def test_owner_and_session_mismatches_fold_to_invalid_without_disclosing_expiry() -> None:
    client = RecordingDynamo()
    store = _store(client)
    store.put(PAGE_RECORD)
    later = PageContextRedemptionRequest(REFERENCE, "owner", "session", EXPIRY)

    assert _redeem(store).labels == LABELS
    assert _redeem(store, owner="another-owner").outcome is PageContextRedemptionOutcome.INVALID
    assert _redeem(store, session="another-session").outcome is (
        PageContextRedemptionOutcome.INVALID
    )
    assert store.read_page_context(later).outcome is PageContextRedemptionOutcome.EXPIRED
    # A mismatch is `invalid` even once the record has expired: expiry is never disclosed.
    assert (
        store.read_page_context(replace(later, verified_owner_id="another-owner")).outcome
        is PageContextRedemptionOutcome.INVALID
    )
    assert all(get["ConsistentRead"] is True for get in client.gets)


@pytest.mark.covers("ADR-120")
def test_redemption_reads_page_items_only_and_refuses_a_v1_snapshot() -> None:
    client = RecordingDynamo()
    store = _store(client)
    parent, _ = _campaign_parent_capability()
    store.put_artifact(parent)

    # Only an `artifact#` item exists under the reference: a `page#` read never returns it.
    assert _redeem(store).outcome is PageContextRedemptionOutcome.INVALID
    assert [get["Key"][LOOKUP_KEY]["S"] for get in client.gets][-1] == f"page#{REFERENCE.value}"

    v1_client = RecordingDynamo()
    v1_store = _store(v1_client)
    v1_store.put(RECORD)
    assert _redeem(v1_store).outcome is PageContextRedemptionOutcome.INVALID


@pytest.mark.covers("ADR-120")
def test_a_failed_page_put_is_the_existing_unwritable_error() -> None:
    store = _store(RecordingDynamo(fail_put=True))

    with pytest.raises(DependencyError, match="could not be recorded"):
        store.put(PAGE_RECORD)
