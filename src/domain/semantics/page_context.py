# SPDX-License-Identifier: MIT-0
"""Short-lived page and selection custody."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from domain.semantics.analytical_resolution import ResolutionOutcome
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    MarketplaceKey,
    ProductKey,
)
from domain.shared_kernel.values import DateRange

__all__ = [
    "MAX_PAGE_CONTEXT_LABEL_CHARS",
    "CampaignSelectionReadRequest",
    "CampaignSelectionValidationResult",
    "PageContextDisplayLabels",
    "PageContextLabels",
    "PageContextRecord",
    "PageContextRedemption",
    "PageContextRedemptionOutcome",
    "PageContextRedemptionRequest",
    "PageContextReference",
    "PageContextSnapshot",
    "ProductSelectionReadRequest",
    "ProductSelectionValidationResult",
    "ReportingWindowStatus",
    "ServerValidatedCampaignSelection",
    "ServerValidatedProductSelection",
    "validate_campaign_selection",
    "validate_page_context",
    "validate_product_selection",
]

_PAGE_CONTEXT_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PAGE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
"""A declared preset id; calendar preset ids carry underscores (ADR-100 point 4)."""
MAX_PAGE_CONTEXT_LABEL_CHARS = 400
"""A stored label's bound. The Harness block truncates each label further, to 200 (decision 6)."""


@dataclass(frozen=True, slots=True)
class PageContextReference:
    """Opaque 256-bit-token shape used only as a server-side lookup reference."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.value), str) or not _PAGE_CONTEXT_REFERENCE.fullmatch(
            self.value
        ):
            raise ValueError(
                "page context reference must be an opaque 43-character base64url token."
            )


class ReportingWindowStatus(StrEnum):
    """How the server obtained the exact effective reporting period."""

    PREFERRED_DEFAULT = "preferred_default"
    PARTIAL_DEFAULT = "partial_default"
    USER_SELECTED = "user_selected"


@dataclass(frozen=True, slots=True)
class PageContextDisplayLabels:
    """ADR-120 decision 2: the governed display text of each filled snapshot field.

    Written by the presentation handler from the names it served, so redemption needs no name
    lookup. Campaign and product names are advertiser-authored data, never instructions.
    """

    page: str
    window: str
    channel: str | None = None
    campaign: str | None = None
    product: str | None = None
    marketplace: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("page", self.page), ("window", self.window)):
            _require_label(value, label)
        for label, value in (
            ("channel", self.channel),
            ("campaign", self.campaign),
            ("product", self.product),
            ("marketplace", self.marketplace),
        ):
            if value is not None:
                _require_label(value, label)

    def items(self) -> tuple[tuple[str, str], ...]:
        """Return the filled labels in the Harness block's fixed order, leaving absent ones out."""
        return tuple(
            (name, value)
            for name, value in (
                ("page", self.page),
                ("window", self.window),
                ("channel", self.channel),
                ("campaign", self.campaign),
                ("product", self.product),
                ("marketplace", self.marketplace),
            )
            if value is not None
        )


@dataclass(frozen=True, slots=True)
class PageContextLabels:
    """Snapshot v2 (ADR-120 decision 2): which page and preset was shown, and its chip text."""

    page_id: str
    page_context_label: str
    display_labels: PageContextDisplayLabels
    preset_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.page_id), str) or not _PAGE_ID.fullmatch(
            self.page_id
        ):
            raise ValueError("page context page id must be a declared page identity.")
        if self.preset_id is not None and (
            not isinstance(cast("object", self.preset_id), str)
            or not _PRESET_ID.fullmatch(self.preset_id)
        ):
            raise ValueError("page context preset id must be a declared preset identity.")
        _require_label(self.page_context_label, "page context label")
        if not isinstance(cast("object", self.display_labels), PageContextDisplayLabels):
            raise TypeError("page context labels require PageContextDisplayLabels.")


@dataclass(frozen=True, slots=True)
class PageContextSnapshot:
    """Canonical selectors and periods captured from one exact server response.

    `labels` is present on a snapshot v2 (ADR-120 decision 2), the only form a conversation
    turn can redeem; a v1 snapshot carries selectors only.
    """

    response_identity: str
    visible_window: DateRange
    reporting_window_status: ReportingWindowStatus
    comparison_window: DateRange | None = None
    campaign: CampaignKey | None = None
    product: ProductKey | None = None
    channel: ChannelKey | None = None
    marketplace: MarketplaceKey | None = None
    labels: PageContextLabels | None = None

    def __post_init__(self) -> None:
        if self.labels is not None and not isinstance(
            cast("object", self.labels), PageContextLabels
        ):
            raise TypeError("page context snapshot labels must be PageContextLabels.")
        if (
            not isinstance(cast("object", self.response_identity), str)
            or not self.response_identity.strip()
        ):
            raise ValueError("page context response identity cannot be blank.")
        if not isinstance(cast("object", self.reporting_window_status), ReportingWindowStatus):
            raise TypeError("page context reporting window status is invalid.")
        visible_window = cast("object", self.visible_window)
        if self.comparison_window is not None and visible_window is None:
            raise ValueError("a comparison window requires a visible window.")
        if not isinstance(visible_window, DateRange):
            raise TypeError("page context visible window must be a DateRange.")
        if self.comparison_window is not None and not isinstance(
            cast("object", self.comparison_window), DateRange
        ):
            raise TypeError("page context comparison window must be a DateRange.")
        if self.campaign is not None and not isinstance(cast("object", self.campaign), CampaignKey):
            raise TypeError("page context campaign must be a CampaignKey.")
        if self.product is not None and not isinstance(cast("object", self.product), ProductKey):
            raise TypeError("page context product must be a ProductKey.")
        if self.channel is not None and not isinstance(cast("object", self.channel), ChannelKey):
            raise TypeError("page context channel must be a ChannelKey.")
        if self.marketplace is not None and not isinstance(
            cast("object", self.marketplace), MarketplaceKey
        ):
            raise TypeError("page context marketplace must be a MarketplaceKey.")


@dataclass(frozen=True, slots=True)
class PageContextRecord:
    """Stored snapshot bound to its opaque reference, verified owner, and conversation."""

    reference: PageContextReference
    snapshot: PageContextSnapshot
    verified_owner_id: str
    conversation_session_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.reference), PageContextReference):
            raise TypeError("page context record requires a PageContextReference.")
        if not isinstance(cast("object", self.snapshot), PageContextSnapshot):
            raise TypeError("page context record requires a PageContextSnapshot.")
        _require_nonblank_binding(self.verified_owner_id, "verified owner id")
        _require_nonblank_binding(self.conversation_session_id, "conversation session id")
        if not _is_aware(self.expires_at):
            raise ValueError("page context expiry must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class CampaignSelectionReadRequest:
    """Exact owner/session custody and instant used to read a campaign selection."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.reference), PageContextReference):
            raise TypeError("campaign selection read requires a PageContextReference.")
        _require_nonblank_binding(self.verified_owner_id, "verified owner id")
        _require_nonblank_binding(self.conversation_session_id, "conversation session id")
        if not _is_aware(self.at):
            raise ValueError("campaign selection read instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class ProductSelectionReadRequest:
    """Exact owner/session custody and instant used to read a product selection."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.reference), PageContextReference):
            raise TypeError("product selection read requires a PageContextReference.")
        _require_nonblank_binding(self.verified_owner_id, "verified owner id")
        _require_nonblank_binding(self.conversation_session_id, "conversation session id")
        if not _is_aware(self.at):
            raise ValueError("product selection read instant must be timezone-aware.")


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedCampaignSelection:
    """Campaign snapshot whose exact owner/session custody has been validated."""

    reference: PageContextReference
    snapshot: PageContextSnapshot

    def __init__(self) -> None:
        raise TypeError("ServerValidatedCampaignSelection can only be created by validation.")


@dataclass(frozen=True, slots=True)
class CampaignSelectionValidationResult:
    """Validated campaign selection or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    selection: ServerValidatedCampaignSelection | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {
            ResolutionOutcome.RESOLVED,
            ResolutionOutcome.INVALID_CONTEXT,
        }:
            raise ValueError("campaign selection validation has only resolved or invalid outcomes.")
        if (self.selection is not None) != (self.outcome is ResolutionOutcome.RESOLVED):
            raise ValueError("only resolved campaign selection validation carries a selection.")


@dataclass(frozen=True, slots=True, init=False)
class ServerValidatedProductSelection:
    """Product snapshot whose exact owner/session custody has been validated."""

    reference: PageContextReference
    snapshot: PageContextSnapshot

    def __init__(self) -> None:
        raise TypeError("ServerValidatedProductSelection can only be created by validation.")


@dataclass(frozen=True, slots=True)
class ProductSelectionValidationResult:
    """Validated product selection or one nondisclosing invalid-context outcome."""

    outcome: ResolutionOutcome
    selection: ServerValidatedProductSelection | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {
            ResolutionOutcome.RESOLVED,
            ResolutionOutcome.INVALID_CONTEXT,
        }:
            raise ValueError("product selection validation has only resolved or invalid outcomes.")
        if (self.selection is not None) != (self.outcome is ResolutionOutcome.RESOLVED):
            raise ValueError("only resolved product selection validation carries a selection.")


def validate_campaign_selection(
    record: PageContextRecord | None,
    request: CampaignSelectionReadRequest,
) -> CampaignSelectionValidationResult:
    """Validate selection custody without disclosing which exact check failed."""
    if (
        record is None
        or record.reference != request.reference
        or record.verified_owner_id != request.verified_owner_id
        or record.conversation_session_id != request.conversation_session_id
        or record.expires_at <= request.at
        or record.snapshot.campaign is None
    ):
        return CampaignSelectionValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    selection = object.__new__(ServerValidatedCampaignSelection)
    object.__setattr__(selection, "reference", record.reference)
    object.__setattr__(selection, "snapshot", record.snapshot)
    return CampaignSelectionValidationResult(ResolutionOutcome.RESOLVED, selection)


def validate_product_selection(
    record: PageContextRecord | None,
    request: ProductSelectionReadRequest,
) -> ProductSelectionValidationResult:
    """Validate product-selection custody without disclosing the failed check."""
    if (
        record is None
        or record.reference != request.reference
        or record.verified_owner_id != request.verified_owner_id
        or record.conversation_session_id != request.conversation_session_id
        or record.expires_at <= request.at
        or record.snapshot.product is None
    ):
        return ProductSelectionValidationResult(ResolutionOutcome.INVALID_CONTEXT)
    selection = object.__new__(ServerValidatedProductSelection)
    object.__setattr__(selection, "reference", record.reference)
    object.__setattr__(selection, "snapshot", record.snapshot)
    return ProductSelectionValidationResult(ResolutionOutcome.RESOLVED, selection)


class PageContextRedemptionOutcome(StrEnum):
    """ADR-120 decision 5: the three redemption results a conversation turn can see."""

    RESOLVED = "resolved"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class PageContextRedemptionRequest:
    """One verified owner and session redeeming a page reference at one instant."""

    reference: PageContextReference
    verified_owner_id: str
    conversation_session_id: str
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.reference), PageContextReference):
            raise TypeError("page context redemption requires a PageContextReference.")
        _require_nonblank_binding(self.verified_owner_id, "verified owner id")
        _require_nonblank_binding(self.conversation_session_id, "conversation session id")
        if not _is_aware(self.at):
            raise ValueError("page context redemption instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class PageContextRedemption:
    """A redeemed page's labels, or the non-disclosing reason it can't be used."""

    outcome: PageContextRedemptionOutcome
    labels: PageContextLabels | None = None

    def __post_init__(self) -> None:
        if (self.labels is not None) != (self.outcome is PageContextRedemptionOutcome.RESOLVED):
            raise ValueError("only a resolved page context redemption carries labels.")


def validate_page_context(
    record: PageContextRecord | None,
    request: PageContextRedemptionRequest,
) -> PageContextRedemption:
    """Redeem one `page#` record without disclosing another session's state.

    A missing record, an owner or session mismatch, or a v1 snapshot with no labels is
    `invalid`, checked before expiry, so `expired` is only ever this session's own record.
    """
    if (
        record is None
        or record.reference != request.reference
        or record.verified_owner_id != request.verified_owner_id
        or record.conversation_session_id != request.conversation_session_id
        or record.snapshot.labels is None
    ):
        return PageContextRedemption(PageContextRedemptionOutcome.INVALID)
    if record.expires_at <= request.at:
        return PageContextRedemption(PageContextRedemptionOutcome.EXPIRED)
    return PageContextRedemption(PageContextRedemptionOutcome.RESOLVED, record.snapshot.labels)


def _require_label(value: str, label: str) -> None:
    if (
        not isinstance(cast("object", value), str)
        or not value.strip()
        or len(value) > MAX_PAGE_CONTEXT_LABEL_CHARS
    ):
        raise ValueError(
            f"page context {label} must be 1 to {MAX_PAGE_CONTEXT_LABEL_CHARS} characters."
        )


def _require_nonblank_binding(value: str, label: str) -> None:
    if not isinstance(cast("object", value), str) or not value.strip():
        raise ValueError(f"page context {label} cannot be blank.")


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
