# SPDX-License-Identifier: MIT-0
"""Immutable exact-key DynamoDB storage for page artifacts and selection capabilities."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn

import boto3
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.items import ItemError, read_text, write_int, write_text
from adapters.page_artifact_codec import (
    campaign_selection_capability_record_from_json,
    campaign_selection_capability_record_to_json,
    channel_row_capability_record_from_json,
    channel_row_capability_record_to_json,
    page_artifact_record_from_json,
    page_artifact_record_to_json,
)
from adapters.page_context_codec import (
    page_context_record_from_json,
    page_context_record_to_json,
)
from adapters.presentation_capability_codec import (
    media_scope_capability_record_from_json,
    media_scope_capability_record_to_json,
    media_window_capability_record_from_json,
    media_window_capability_record_to_json,
    product_selection_capability_record_from_json,
    product_selection_capability_record_to_json,
    visual_narration_reference_record_from_json,
    visual_narration_reference_record_to_json,
)
from domain.presentation.artifacts import (
    CampaignSelectionCapabilityReadRequest,
    CampaignSelectionCapabilityRecord,
    CampaignSelectionCapabilityValidationResult,
    ChannelRowCapabilityReadRequest,
    ChannelRowCapabilityRecord,
    ChannelRowCapabilityValidationResult,
    MediaScopeCapabilityReadRequest,
    MediaScopeCapabilityRecord,
    MediaScopeCapabilityValidationResult,
    MediaWindowCapabilityReadRequest,
    MediaWindowCapabilityRecord,
    MediaWindowCapabilityValidationResult,
    PageArtifactReadRequest,
    PageArtifactRecord,
    PageArtifactValidationResult,
    ParentCapabilityIssuanceOutcome,
    ParentCapabilityIssuanceRequest,
    ParentCapabilityIssuanceResult,
    ParentCapabilityRecord,
    ProductSelectionCapabilityReadRequest,
    ProductSelectionCapabilityRecord,
    ProductSelectionCapabilityValidationResult,
    ServerValidatedPageArtifact,
    VisualNarrationReferenceReadRequest,
    VisualNarrationReferenceRecord,
    VisualNarrationReferenceValidationResult,
    campaign_capability_parent_is_valid,
    channel_capability_parent_is_valid,
    media_scope_capability_parent_is_valid,
    media_window_capability_parent_is_valid,
    page_artifact_parent_is_valid,
    product_capability_parent_is_valid,
    validate_campaign_selection_capability,
    validate_channel_row_capability,
    validate_media_scope_capability,
    validate_media_window_capability,
    validate_page_artifact,
    validate_product_selection_capability,
    validate_visual_narration_reference,
    visual_narration_reference_parent_is_valid,
)
from domain.semantics.analytical_resolution import ResolutionOutcome
from domain.semantics.page_context import (
    CampaignSelectionReadRequest,
    CampaignSelectionValidationResult,
    PageContextRecord,
    PageContextRedemption,
    PageContextRedemptionRequest,
    PageContextReference,
    ProductSelectionReadRequest,
    ProductSelectionValidationResult,
    validate_campaign_selection,
    validate_page_context,
    validate_product_selection,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.page_artifact import (
    PageArtifactStorePort,
    ParentCapabilityIssuancePort,
    SelectionCapabilityStorePort,
    VisualNarrationReferenceStorePort,
)
from ports.outbound.page_context import PageContextStorePort

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

__all__ = [
    "CAMPAIGN_CAPABILITY_PREFIX",
    "CHANNEL_CAPABILITY_PREFIX",
    "EXPIRES_AT",
    "LOOKUP_KEY",
    "MEDIA_SCOPE_CAPABILITY_PREFIX",
    "MEDIA_WINDOW_CAPABILITY_PREFIX",
    "PRODUCT_CAPABILITY_PREFIX",
    "VISUAL_NARRATION_REFERENCE_PREFIX",
    "DynamoPageContextStore",
]

LOOKUP_KEY = "lookup_key"
EXPIRES_AT = "expires_at"
PAYLOAD = "payload"
ARTIFACT_PREFIX = "artifact#"
CAMPAIGN_CAPABILITY_PREFIX = "campaign-capability#"
CHANNEL_CAPABILITY_PREFIX = "channel-capability#"
PRODUCT_CAPABILITY_PREFIX = "product-capability#"
MEDIA_SCOPE_CAPABILITY_PREFIX = "media-scope-capability#"
MEDIA_WINDOW_CAPABILITY_PREFIX = "media-window-capability#"
VISUAL_NARRATION_REFERENCE_PREFIX = "visual-narration-reference#"
CURRENT_REFERENCE_PREFIX = "current-reference#"
PAGE_PREFIX = "page#"
SELECTION_PREFIX = "selection#"
TURN_PREFIX = "turn#"
PAGE_CONTEXT_UNWRITABLE = ErrorCode("PAGE_CONTEXT", 1)
PAGE_CONTEXT_UNREADABLE = ErrorCode("PAGE_CONTEXT", 2)
PAGE_CONTEXT_CONFLICT = ErrorCode("PAGE_CONTEXT", 3)
PAGE_CONTEXT_MALFORMED = ErrorCode("PAGE_CONTEXT", 4)
PAGE_CONTEXT_INCONSISTENT = ErrorCode("PAGE_CONTEXT", 5)
MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS = 51
MAX_DYNAMODB_ITEM_BYTES = 400_000
MAX_DYNAMODB_TRANSACTION_BYTES = 4_000_000
CONDITIONAL = "ConditionalCheckFailedException"
TRANSACTION_CANCELLED = "TransactionCanceledException"
TRANSACTION_CONDITIONAL = "ConditionalCheckFailed"


def parent_capability_transaction_fits(
    items: tuple[dict[str, AttributeValueTypeDef], ...],
) -> bool:
    """Conservatively enforce DynamoDB's explicit item and transaction byte bounds."""
    encoded = tuple(
        len(json.dumps(item, separators=(",", ":"), sort_keys=True).encode()) for item in items
    )
    return (
        len(items) <= MAX_PARENT_CAPABILITY_TRANSACTION_ITEMS
        and all(size <= MAX_DYNAMODB_ITEM_BYTES for size in encoded)
        and sum(encoded) <= MAX_DYNAMODB_TRANSACTION_BYTES
    )


def _transaction_is_conditional(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == TRANSACTION_CANCELLED and any(
        reason.get("Code") == TRANSACTION_CONDITIONAL
        for reason in error.response.get("CancellationReasons", [])
    )


def _page_key(record: PageContextRecord) -> str:
    return f"{PAGE_PREFIX}{record.reference.value}"


def _item(key: str, expires_at: int, payload: str) -> dict[str, AttributeValueTypeDef]:
    return {
        LOOKUP_KEY: write_text(key),
        EXPIRES_AT: write_int(expires_at),
        PAYLOAD: write_text(payload),
    }


def _page_item(record: PageContextRecord) -> dict[str, AttributeValueTypeDef]:
    return _item(
        _page_key(record),
        int(record.expires_at.timestamp()),
        page_context_record_to_json(record),
    )


def _artifact_item(record: PageArtifactRecord) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{ARTIFACT_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        page_artifact_record_to_json(record),
    )


def _campaign_capability_item(
    record: CampaignSelectionCapabilityRecord,
) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{CAMPAIGN_CAPABILITY_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        campaign_selection_capability_record_to_json(record),
    )


def _channel_capability_item(
    record: ChannelRowCapabilityRecord,
) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{CHANNEL_CAPABILITY_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        channel_row_capability_record_to_json(record),
    )


def _product_capability_item(
    record: ProductSelectionCapabilityRecord,
) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{PRODUCT_CAPABILITY_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        product_selection_capability_record_to_json(record),
    )


def _media_scope_capability_item(
    record: MediaScopeCapabilityRecord,
) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{MEDIA_SCOPE_CAPABILITY_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        media_scope_capability_record_to_json(record),
    )


def _media_window_capability_item(
    record: MediaWindowCapabilityRecord,
) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{MEDIA_WINDOW_CAPABILITY_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        media_window_capability_record_to_json(record),
    )


def _visual_narration_reference_item(
    record: VisualNarrationReferenceRecord,
) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{VISUAL_NARRATION_REFERENCE_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        visual_narration_reference_record_to_json(record),
    )


def _issuance_capability_item(
    record: ParentCapabilityRecord,
) -> dict[str, AttributeValueTypeDef]:
    if isinstance(record, CampaignSelectionCapabilityRecord):
        return _campaign_capability_item(record)
    if isinstance(record, ChannelRowCapabilityRecord):
        return _channel_capability_item(record)
    if isinstance(record, ProductSelectionCapabilityRecord):
        return _product_capability_item(record)
    if isinstance(record, MediaScopeCapabilityRecord):
        return _media_scope_capability_item(record)
    if isinstance(record, MediaWindowCapabilityRecord):
        return _media_window_capability_item(record)
    return _visual_narration_reference_item(record)


def _selection_item(record: PageContextRecord) -> dict[str, AttributeValueTypeDef]:
    return _item(
        f"{SELECTION_PREFIX}{record.reference.value}",
        int(record.expires_at.timestamp()),
        page_context_record_to_json(record),
    )


class DynamoPageContextStore(
    PageContextStorePort,
    PageArtifactStorePort,
    ParentCapabilityIssuancePort,
    SelectionCapabilityStorePort,
    VisualNarrationReferenceStorePort,
):
    """Strongly read and immutably store page artifacts and selection custody."""

    def __init__(
        self,
        table_name: str,
        client: DynamoDBClient | None = None,
    ) -> None:
        if not table_name.strip():
            raise ValueError("page context table name is required.")
        self._table = table_name
        self._client = client

    @property
    def _dynamodb(self) -> DynamoDBClient:
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def put(self, record: PageContextRecord) -> None:
        """Create one owner/session-bound page record without permitting replacement."""
        self._put_immutable(_page_item(record))

    def put_artifact(self, record: PageArtifactRecord) -> None:
        """Create one typed artifact, attesting an optional parent before any write."""
        if record.parent_reference is not None:
            parent = self._read_artifact_record(record.parent_reference)
            if parent is None or not page_artifact_parent_is_valid(record, parent):
                self._raise_inconsistent("artifact")
        self._put_immutable(_artifact_item(record))

    def read_artifact(self, request: PageArtifactReadRequest) -> PageArtifactValidationResult:
        """Strongly read and validate one exact artifact plus optional parent custody."""
        record = self._read_artifact_record(request.reference)
        validation = validate_page_artifact(record, request)
        artifact = validation.artifact
        if record is None or artifact is None or artifact.parent_reference is None:
            return validation
        parent_record = self._read_artifact_record(artifact.parent_reference)
        parent_validation = validate_page_artifact(
            parent_record,
            PageArtifactReadRequest(
                artifact.parent_reference,
                request.verified_owner_id,
                request.conversation_session_id,
                request.at,
            ),
        )
        if parent_record is None or parent_validation.artifact is None:
            return PageArtifactValidationResult(ResolutionOutcome.INVALID_CONTEXT)
        if not page_artifact_parent_is_valid(record, parent_record):
            self._raise_inconsistent("artifact")
        return validation

    def issue_parent_capabilities(
        self, request: ParentCapabilityIssuanceRequest
    ) -> ParentCapabilityIssuanceResult:
        """Atomically write one parent and its complete bounded capability set."""
        if request.parent.parent_reference is not None:
            parent = self._read_artifact_record(request.parent.parent_reference)
            if parent is None or not page_artifact_parent_is_valid(request.parent, parent):
                self._raise_inconsistent("artifact")
        items = (
            _artifact_item(request.parent),
            *(_issuance_capability_item(record) for record in request.capabilities),
        )
        if not parent_capability_transaction_fits(items):
            return ParentCapabilityIssuanceResult(ParentCapabilityIssuanceOutcome.CAPACITY_REFUSED)
        transaction: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": item,
                    "ConditionExpression": f"attribute_not_exists({LOOKUP_KEY})",
                }
            }
            for item in items
        ]
        try:
            self._dynamodb.transact_write_items(TransactItems=transaction)
        except Exception as error:
            if isinstance(error, ClientError) and _transaction_is_conditional(error):
                if self._exact_transaction_replay(items):
                    return ParentCapabilityIssuanceResult(ParentCapabilityIssuanceOutcome.ISSUED)
                raise InvariantError(
                    PAGE_CONTEXT_CONFLICT,
                    "A page-context lookup key names different immutable bytes.",
                    context={"record_kind": "parent-capability-issuance"},
                ) from None
            raise DependencyError(
                PAGE_CONTEXT_UNWRITABLE,
                "Page context could not be recorded.",
                context={"cause": type(error).__name__},
            ) from None
        return ParentCapabilityIssuanceResult(ParentCapabilityIssuanceOutcome.ISSUED)

    def put_campaign_capability(self, record: CampaignSelectionCapabilityRecord) -> None:
        """Create one campaign capability only after attesting its parent artifact."""
        parent = self._read_artifact_record(record.parent_artifact_reference)
        if parent is None or not campaign_capability_parent_is_valid(record, parent):
            self._raise_inconsistent("campaign-capability")
        self._put_immutable(_campaign_capability_item(record))

    def read_campaign_capability(
        self, request: CampaignSelectionCapabilityReadRequest
    ) -> CampaignSelectionCapabilityValidationResult:
        """Strongly redeem one campaign capability and its exact parent revision."""
        record = self._read_campaign_capability_record(request.reference)
        if record is None:
            return validate_campaign_selection_capability(None, request, None)
        parent_record = self._read_artifact_record(record.parent_artifact_reference)
        if parent_record is None:
            return validate_campaign_selection_capability(record, request, None)
        parent_result = validate_page_artifact(
            parent_record,
            PageArtifactReadRequest(
                reference=record.parent_artifact_reference,
                verified_owner_id=request.verified_owner_id,
                conversation_session_id=request.conversation_session_id,
                at=request.at,
            ),
        )
        parent = parent_result.artifact
        if parent is None:
            return validate_campaign_selection_capability(record, request, None)
        if not campaign_capability_parent_is_valid(record, parent_record):
            self._raise_inconsistent("campaign-capability")
        return validate_campaign_selection_capability(record, request, parent)

    def put_channel_capability(self, record: ChannelRowCapabilityRecord) -> None:
        """Create one exact row capability only after attesting its parent artifact."""
        parent = self._read_artifact_record(record.parent_artifact_reference)
        if parent is None or not channel_capability_parent_is_valid(record, parent):
            self._raise_inconsistent("channel-capability")
        self._put_immutable(_channel_capability_item(record))

    def read_channel_capability(
        self, request: ChannelRowCapabilityReadRequest
    ) -> ChannelRowCapabilityValidationResult:
        """Strongly redeem one opaque channel capability and its exact parent revision."""
        record = self._read_channel_capability_record(request.reference)
        if record is None:
            return validate_channel_row_capability(None, request, None)
        parent_record = self._read_artifact_record(record.parent_artifact_reference)
        if parent_record is None:
            return validate_channel_row_capability(record, request, None)
        parent_result = validate_page_artifact(
            parent_record,
            PageArtifactReadRequest(
                reference=record.parent_artifact_reference,
                verified_owner_id=request.verified_owner_id,
                conversation_session_id=request.conversation_session_id,
                at=request.at,
            ),
        )
        parent = parent_result.artifact
        if parent is None:
            return validate_channel_row_capability(record, request, None)
        if not channel_capability_parent_is_valid(record, parent_record):
            self._raise_inconsistent("channel-capability")
        return validate_channel_row_capability(record, request, parent)

    def put_product_capability(self, record: ProductSelectionCapabilityRecord) -> None:
        """Create one Product capability only after attesting its parent artifact."""
        parent = self._read_artifact_record(record.parent_artifact_reference)
        if parent is None or not product_capability_parent_is_valid(record, parent):
            self._raise_inconsistent("product-capability")
        self._put_immutable(_product_capability_item(record))

    def read_product_capability(
        self, request: ProductSelectionCapabilityReadRequest
    ) -> ProductSelectionCapabilityValidationResult:
        """Strongly redeem one Product capability and its exact parent revision."""
        record = self._read_product_capability_record(request.reference)
        if record is None:
            return validate_product_selection_capability(None, request, None)
        parent_record = self._read_artifact_record(record.parent_artifact_reference)
        parent = self._validated_parent_for_capability(parent_record, record, request)
        if parent_record is None or parent is None:
            return validate_product_selection_capability(record, request, None)
        if not product_capability_parent_is_valid(record, parent_record):
            self._raise_inconsistent("product-capability")
        return validate_product_selection_capability(record, request, parent)

    def put_media_scope_capability(self, record: MediaScopeCapabilityRecord) -> None:
        """Create one Media scope capability only after attesting its parent artifact."""
        parent = self._read_artifact_record(record.parent_artifact_reference)
        if parent is None or not media_scope_capability_parent_is_valid(record, parent):
            self._raise_inconsistent("media-scope-capability")
        self._put_immutable(_media_scope_capability_item(record))

    def read_media_scope_capability(
        self, request: MediaScopeCapabilityReadRequest
    ) -> MediaScopeCapabilityValidationResult:
        """Strongly redeem one Media scope capability and its exact parent revision."""
        record = self._read_media_scope_capability_record(request.reference)
        if record is None:
            return validate_media_scope_capability(None, request, None)
        parent_record = self._read_artifact_record(record.parent_artifact_reference)
        parent = self._validated_parent_for_capability(parent_record, record, request)
        if parent_record is None or parent is None:
            return validate_media_scope_capability(record, request, None)
        if not media_scope_capability_parent_is_valid(record, parent_record):
            self._raise_inconsistent("media-scope-capability")
        return validate_media_scope_capability(record, request, parent)

    def put_media_window_capability(self, record: MediaWindowCapabilityRecord) -> None:
        """Create one Media window capability only after attesting its parent artifact."""
        parent = self._read_artifact_record(record.parent_artifact_reference)
        if parent is None or not media_window_capability_parent_is_valid(record, parent):
            self._raise_inconsistent("media-window-capability")
        self._put_immutable(_media_window_capability_item(record))

    def read_media_window_capability(
        self, request: MediaWindowCapabilityReadRequest
    ) -> MediaWindowCapabilityValidationResult:
        """Strongly redeem one Media window capability and its exact parent revision."""
        record = self._read_media_window_capability_record(request.reference)
        if record is None:
            return validate_media_window_capability(None, request, None)
        parent_record = self._read_artifact_record(record.parent_artifact_reference)
        parent = self._validated_parent_for_capability(parent_record, record, request)
        if parent_record is None or parent is None:
            return validate_media_window_capability(record, request, None)
        if not media_window_capability_parent_is_valid(record, parent_record):
            self._raise_inconsistent("media-window-capability")
        return validate_media_window_capability(record, request, parent)

    def put_visual_narration_reference(self, record: VisualNarrationReferenceRecord) -> None:
        """Create one narration reference only after attesting its parent artifact."""
        parent = self._read_artifact_record(record.parent_artifact_reference)
        if parent is None or not visual_narration_reference_parent_is_valid(record, parent):
            self._raise_inconsistent("visual-narration-reference")
        self._put_immutable(_visual_narration_reference_item(record))

    def read_visual_narration_reference(
        self, request: VisualNarrationReferenceReadRequest
    ) -> VisualNarrationReferenceValidationResult:
        """Strongly redeem one visual narration reference and its parent revision."""
        record = self._read_visual_narration_reference_record(request.reference)
        if record is None:
            return validate_visual_narration_reference(None, request, None)
        parent_record = self._read_artifact_record(record.parent_artifact_reference)
        parent = self._validated_parent_for_capability(parent_record, record, request)
        if parent_record is None or parent is None:
            return validate_visual_narration_reference(record, request, None)
        if not visual_narration_reference_parent_is_valid(record, parent_record):
            self._raise_inconsistent("visual-narration-reference")
        return validate_visual_narration_reference(record, request, parent)

    def put_selection(self, record: PageContextRecord) -> None:
        """Create one owner/session-bound selection without permitting replacement."""
        self._put_immutable(_selection_item(record))

    def read_selection(
        self, request: CampaignSelectionReadRequest
    ) -> CampaignSelectionValidationResult:
        """Strongly read and validate one exact campaign-selection record."""
        record = self._read_selection_record(request.reference)
        return validate_campaign_selection(record, request)

    def read_product_selection(
        self, request: ProductSelectionReadRequest
    ) -> ProductSelectionValidationResult:
        """Strongly read and validate one exact product-selection record."""
        record = self._read_selection_record(request.reference)
        return validate_product_selection(record, request)

    def read_page_context(self, request: PageContextRedemptionRequest) -> PageContextRedemption:
        """Strongly read `page#` only; a `page#` read never returns artifact bytes (ADR-120)."""
        return validate_page_context(self._read_page_record(request.reference), request)

    def _read_page_record(self, reference: PageContextReference) -> PageContextRecord | None:
        item = self._get(f"{PAGE_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = page_context_record_from_json(read_text(item, PAYLOAD))
            if item != _page_item(record):
                raise ValueError("page envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("page")
        return record

    def _read_artifact_record(self, reference: PageContextReference) -> PageArtifactRecord | None:
        item = self._get(f"{ARTIFACT_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = page_artifact_record_from_json(read_text(item, PAYLOAD))
            if item != _artifact_item(record):
                raise ValueError("artifact envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("artifact")
        return record

    def _read_campaign_capability_record(
        self, reference: PageContextReference
    ) -> CampaignSelectionCapabilityRecord | None:
        item = self._get(f"{CAMPAIGN_CAPABILITY_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = campaign_selection_capability_record_from_json(read_text(item, PAYLOAD))
            if item != _campaign_capability_item(record):
                raise ValueError("campaign capability envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("campaign-capability")
        return record

    def _read_channel_capability_record(
        self, reference: PageContextReference
    ) -> ChannelRowCapabilityRecord | None:
        item = self._get(f"{CHANNEL_CAPABILITY_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = channel_row_capability_record_from_json(read_text(item, PAYLOAD))
            if item != _channel_capability_item(record):
                raise ValueError("channel capability envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("channel-capability")
        return record

    def _read_product_capability_record(
        self, reference: PageContextReference
    ) -> ProductSelectionCapabilityRecord | None:
        item = self._get(f"{PRODUCT_CAPABILITY_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = product_selection_capability_record_from_json(read_text(item, PAYLOAD))
            if item != _product_capability_item(record):
                raise ValueError("product capability envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("product-capability")
        return record

    def _read_media_scope_capability_record(
        self, reference: PageContextReference
    ) -> MediaScopeCapabilityRecord | None:
        item = self._get(f"{MEDIA_SCOPE_CAPABILITY_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = media_scope_capability_record_from_json(read_text(item, PAYLOAD))
            if item != _media_scope_capability_item(record):
                raise ValueError("media scope capability envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("media-scope-capability")
        return record

    def _read_media_window_capability_record(
        self, reference: PageContextReference
    ) -> MediaWindowCapabilityRecord | None:
        item = self._get(f"{MEDIA_WINDOW_CAPABILITY_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = media_window_capability_record_from_json(read_text(item, PAYLOAD))
            if item != _media_window_capability_item(record):
                raise ValueError("media window capability envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("media-window-capability")
        return record

    def _read_visual_narration_reference_record(
        self, reference: PageContextReference
    ) -> VisualNarrationReferenceRecord | None:
        item = self._get(f"{VISUAL_NARRATION_REFERENCE_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = visual_narration_reference_record_from_json(read_text(item, PAYLOAD))
            if item != _visual_narration_reference_item(record):
                raise ValueError("visual narration reference envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("visual-narration-reference")
        return record

    def _validated_parent_for_capability(
        self,
        parent_record: PageArtifactRecord | None,
        record: ParentCapabilityRecord,
        request: CampaignSelectionCapabilityReadRequest
        | ChannelRowCapabilityReadRequest
        | ProductSelectionCapabilityReadRequest
        | MediaScopeCapabilityReadRequest
        | MediaWindowCapabilityReadRequest
        | VisualNarrationReferenceReadRequest,
    ) -> ServerValidatedPageArtifact | None:
        if parent_record is None:
            return None
        result = validate_page_artifact(
            parent_record,
            PageArtifactReadRequest(
                record.parent_artifact_reference,
                request.verified_owner_id,
                request.conversation_session_id,
                request.at,
            ),
        )
        return result.artifact

    def _read_selection_record(self, reference: PageContextReference) -> PageContextRecord | None:
        item = self._get(f"{SELECTION_PREFIX}{reference.value}")
        if item is None:
            return None
        try:
            record = page_context_record_from_json(read_text(item, PAYLOAD))
            if item != _selection_item(record):
                raise ValueError("selection envelope differs from payload")
        except ItemError, TypeError, ValueError:
            self._raise_malformed("selection")
        return record

    def _exact_transaction_replay(
        self, items: tuple[dict[str, AttributeValueTypeDef], ...]
    ) -> bool:
        return all(self._get(read_text(item, LOOKUP_KEY)) == item for item in items)

    def _get(self, key: str) -> dict[str, AttributeValueTypeDef] | None:
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={LOOKUP_KEY: write_text(key)},
                ConsistentRead=True,
            )
        except Exception as error:
            raise DependencyError(
                PAGE_CONTEXT_UNREADABLE,
                "Page context could not be read.",
                context={"cause": type(error).__name__},
            ) from None
        found = response.get("Item")
        return None if found is None else dict(found)

    def _put_immutable(self, item: dict[str, AttributeValueTypeDef]) -> None:
        try:
            self._dynamodb.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression=f"attribute_not_exists({LOOKUP_KEY})",
            )
            return
        except Exception as error:
            if not (
                isinstance(error, ClientError)
                and error.response.get("Error", {}).get("Code") == CONDITIONAL
            ):
                raise DependencyError(
                    PAGE_CONTEXT_UNWRITABLE,
                    "Page context could not be recorded.",
                    context={"cause": type(error).__name__},
                ) from None

        key = read_text(item, LOOKUP_KEY)
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={LOOKUP_KEY: item[LOOKUP_KEY]},
                ConsistentRead=True,
            )
        except Exception as error:
            raise DependencyError(
                PAGE_CONTEXT_UNWRITABLE,
                "Existing page context could not be attested.",
                context={"cause": type(error).__name__},
            ) from None
        existing = response.get("Item")
        if existing is not None and dict(existing) == item:
            return
        if existing is None:
            raise DependencyError(
                PAGE_CONTEXT_UNWRITABLE,
                "Existing page context could not be attested.",
                context={"cause": "MissingConditionalRecord"},
            ) from None
        raise InvariantError(
            PAGE_CONTEXT_CONFLICT,
            "A page-context lookup key names different immutable bytes.",
            context={"record_kind": key.split("#", 1)[0]},
        ) from None

    @staticmethod
    def _raise_malformed(record_kind: str) -> NoReturn:
        raise InvariantError(
            PAGE_CONTEXT_MALFORMED,
            "Persisted page-context bytes are malformed.",
            context={"record_kind": record_kind},
        ) from None

    @staticmethod
    def _raise_inconsistent(record_kind: str) -> NoReturn:
        raise InvariantError(
            PAGE_CONTEXT_INCONSISTENT,
            "Persisted page-context custody is inconsistent.",
            context={"record_kind": record_kind},
        ) from None
