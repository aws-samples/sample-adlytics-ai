# SPDX-License-Identifier: MIT-0
"""Governed Media scope-catalog derivation and current-identity revalidation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_scope_catalog import (
    GovernedMediaScopeCatalog,
    MediaScopeCatalogDependencies,
)
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.source_pack import load_source_packs
from domain.presentation.artifacts import MediaScope
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.values import DateRange
from ports.outbound.media_scope_catalog import (
    MediaScopeCatalogCoverageRequest,
    MediaScopeCatalogListRequest,
    MediaScopeCatalogRevalidationOutcome,
    MediaScopeCatalogRevalidationRequest,
)
from ports.outbound.semantic_state import CurrentPublicationPort

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WINDOW = DateRange(date(2026, 9, 1), date(2026, 9, 7))


@dataclass
class _Current:
    publication_id: str = "fictional-current-publication-a"

    def current_for_scope(
        self, scope: object, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        _ = scope, horizon
        return ()


class _Catalog(GovernedMediaScopeCatalog):
    """Keeps this test focused on catalog derivation, not publication-store mechanics."""

    def __init__(self, current: _Current) -> None:
        bridge = load_commerce_identity_bridge(ROOT)
        super().__init__(
            MediaScopeCatalogDependencies(
                commerce_bridge=bridge,
                taxonomy=load_media_taxonomy(ROOT),
                current=cast("CurrentPublicationPort", current),
                packs=load_source_packs(ROOT),
                contracts=tuple(contract for _, contract in load_contracts(ROOT)),
            )
        )
        self._test_current = current

    def _publication_days(self) -> set[date]:
        return {WINDOW.start + timedelta(days=offset) for offset in range(WINDOW.days)}

    def _current_publications(
        self, pack: object, marketplace: object, window: DateRange
    ) -> tuple[SuccessfulPublication, ...] | None:
        _ = (pack, marketplace, window)
        publication = SimpleNamespace(
            publication_id=SimpleNamespace(value=self._test_current.publication_id)
        )
        return (cast("SuccessfulPublication", publication),)


def test_catalog_derives_literal_dsp_intersection_and_revalidates_identity() -> None:
    current = _Current()
    catalog = _Catalog(current)
    bridge = load_commerce_identity_bridge(ROOT)
    authorized = AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope}))

    assert catalog.coverage(MediaScopeCatalogCoverageRequest(authorized)).intervals == (WINDOW,)

    result = catalog.list_scopes(MediaScopeCatalogListRequest(authorized, WINDOW, 25))

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.scope == MediaScope(
        commerce_scope=bridge.commerce_scope,
        platform=type(candidate.scope.platform)("amazon_ads"),
        channel=type(candidate.scope.channel)("amazon_dsp"),
        marketplace=type(candidate.scope.marketplace)("amazon_us"),
    )
    assert candidate.safe_signature.label == "Amazon Ads · Amazon DSP · United States"
    assert candidate.catalog_identity.startswith("media-catalog-v1:")
    request = MediaScopeCatalogRevalidationRequest(
        authorized, candidate.scope, candidate.catalog_identity, WINDOW
    )
    assert catalog.revalidate(request).outcome is MediaScopeCatalogRevalidationOutcome.CURRENT

    current.publication_id = "fictional-current-publication-b"

    assert (
        catalog.revalidate(request).outcome
        is MediaScopeCatalogRevalidationOutcome.STALE_OR_UNAVAILABLE
    )


class _Missing(_Catalog):
    """Reports no CURRENT publication for one contract's pack."""

    def __init__(self, current: _Current, missing: str) -> None:
        super().__init__(current)
        self._missing = missing

    def _current_publications(
        self, pack: object, marketplace: object, window: DateRange
    ) -> tuple[SuccessfulPublication, ...] | None:
        if getattr(pack, "contract_id", None) == self._missing:
            return None
        return super()._current_publications(pack, marketplace, window)


def test_a_missing_dsp_reach_delivery_never_hides_the_dsp_scope() -> None:
    """Pack 4 is checked per request (plan D6), so it isn't media-scope evidence."""
    bridge = load_commerce_identity_bridge(ROOT)
    authorized = AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope}))
    request = MediaScopeCatalogListRequest(authorized, WINDOW, 25)
    without_reach = _Missing(_Current(), "amc-dsp-reach").list_scopes(request)
    assert [item.safe_signature.label for item in without_reach.candidates] == [
        "Amazon Ads · Amazon DSP · United States"
    ]
    # Its identity is the one the four delivery writers alone give.
    assert without_reach.candidates == _Catalog(_Current()).list_scopes(request).candidates
    # Control: a missing DSP delivery writer still removes the scope.
    assert _Missing(_Current(), "amc-dsp-impressions").list_scopes(request).candidates == ()
