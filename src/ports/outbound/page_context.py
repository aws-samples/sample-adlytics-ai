# SPDX-License-Identifier: MIT-0
"""Short-lived page and selection context storage boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.semantics.page_context import (
    CampaignSelectionReadRequest,
    CampaignSelectionValidationResult,
    PageContextRecord,
    PageContextRedemption,
    PageContextRedemptionRequest,
    ProductSelectionReadRequest,
    ProductSelectionValidationResult,
)

__all__ = ["PageContextStorePort"]


@runtime_checkable
class PageContextStorePort(Protocol):
    """Store immutable page snapshots and exact server-side selections."""

    def put(self, record: PageContextRecord) -> None:
        """Persist one immutable, owner- and session-bound page-context record."""
        ...

    def put_selection(self, record: PageContextRecord) -> None:
        """Persist one immutable, owner- and session-bound selection."""
        ...

    def read_selection(
        self, request: CampaignSelectionReadRequest
    ) -> CampaignSelectionValidationResult:
        """Return exact validated campaign-selection custody or invalid context."""
        ...

    def read_product_selection(
        self, request: ProductSelectionReadRequest
    ) -> ProductSelectionValidationResult:
        """Return exact validated product-selection custody or invalid context."""
        ...

    def read_page_context(self, request: PageContextRedemptionRequest) -> PageContextRedemption:
        """Read `page#` only: resolved labels, `expired` or `invalid` (ADR-120 decision 5).

        The request carries the reference, the verified owner and session, and the instant.
        """
        ...
