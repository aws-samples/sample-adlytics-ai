# SPDX-License-Identifier: MIT-0
"""Read-only media-scope catalog port contract."""

from __future__ import annotations

from datetime import date
from typing import cast

import pytest

from domain.presentation.artifacts import MediaScope, SafeMediaScopeDisplaySignature
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import (
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.media_scope_catalog import (
    MAX_MEDIA_SCOPE_CANDIDATES,
    MediaScopeCatalogCandidate,
    MediaScopeCatalogCoverageRequest,
    MediaScopeCatalogCoverageResult,
    MediaScopeCatalogListRequest,
    MediaScopeCatalogListResult,
    MediaScopeCatalogPort,
    MediaScopeCatalogRevalidationOutcome,
    MediaScopeCatalogRevalidationRequest,
    MediaScopeCatalogRevalidationResult,
)

pytestmark = pytest.mark.unit


COMMERCE_SCOPE = CommerceScopeKey("fictional-commerce-scope")
AUTHORIZED_SCOPE = AuthorizedAnalyticalScope(frozenset({COMMERCE_SCOPE}))
WINDOW = DateRange(date(2026, 9, 1), date(2026, 9, 7))


def _candidate(identity: str = "fictional-catalog-identity") -> MediaScopeCatalogCandidate:
    return MediaScopeCatalogCandidate(
        scope=MediaScope(
            commerce_scope=COMMERCE_SCOPE,
            platform=PlatformKey("fictional-platform"),
            channel=ChannelKey("fictional-channel"),
            marketplace=MarketplaceKey("fictional-marketplace"),
        ),
        safe_signature=SafeMediaScopeDisplaySignature("Fictional media scope"),
        catalog_identity=identity,
    )


class TestMediaScopeCatalogCoverageContract:
    def test_coverage_request_requires_authorized_scope(self) -> None:
        with pytest.raises(TypeError, match="authorized analytical scope"):
            MediaScopeCatalogCoverageRequest(cast("AuthorizedAnalyticalScope", object()))

    def test_coverage_result_rejects_overlapping_intervals(self) -> None:
        with pytest.raises(ValueError, match="sorted and disjoint"):
            MediaScopeCatalogCoverageResult((WINDOW, DateRange(date(2026, 9, 7), date(2026, 9, 8))))


class TestMediaScopeCatalogListContract:
    @pytest.mark.parametrize("max_candidates", [0, MAX_MEDIA_SCOPE_CANDIDATES + 1, True])
    def test_list_request_rejects_an_unbounded_or_noninteger_candidate_limit(
        self, max_candidates: int
    ) -> None:
        with pytest.raises(ValueError, match="candidate limit"):
            MediaScopeCatalogListRequest(AUTHORIZED_SCOPE, WINDOW, max_candidates)

    def test_candidate_rejects_a_blank_stable_identity(self) -> None:
        """A blank identity would make future catalog-current checks ambiguous."""
        with pytest.raises(ValueError, match="stable nonblank identity"):
            _candidate(" ")

    def test_list_result_rejects_repeated_stable_identities(self) -> None:
        """Duplicate identities could mint competing capabilities for one catalog record."""
        duplicate_identity = MediaScopeCatalogCandidate(
            scope=MediaScope(
                commerce_scope=COMMERCE_SCOPE,
                platform=PlatformKey("fictional-other-platform"),
                channel=ChannelKey("fictional-other-channel"),
                marketplace=MarketplaceKey("fictional-marketplace"),
            ),
            safe_signature=SafeMediaScopeDisplaySignature("Fictional other media scope"),
            catalog_identity="fictional-catalog-identity",
        )

        with pytest.raises(ValueError, match="repeats a stable candidate identity"):
            MediaScopeCatalogListResult((_candidate(), duplicate_identity))

    def test_list_result_rejects_more_than_the_bounded_candidate_contract(self) -> None:
        candidates = tuple(_candidate(f"fictional-catalog-identity-{index}") for index in range(26))

        with pytest.raises(ValueError, match="more candidates"):
            MediaScopeCatalogListResult(candidates)


class TestMediaScopeCatalogRevalidationContract:
    def test_revalidation_rejects_a_canonical_scope_outside_the_authorized_scope(self) -> None:
        outside_scope = MediaScope(
            commerce_scope=CommerceScopeKey("another-fictional-commerce-scope"),
            platform=PlatformKey("fictional-platform"),
            channel=ChannelKey("fictional-channel"),
            marketplace=MarketplaceKey("fictional-marketplace"),
        )

        with pytest.raises(ValueError, match="outside the authorized scope"):
            MediaScopeCatalogRevalidationRequest(
                AUTHORIZED_SCOPE, outside_scope, "fictional-catalog-identity", WINDOW
            )

    def test_revalidation_result_carries_only_a_closed_nondisclosing_outcome(self) -> None:
        result = MediaScopeCatalogRevalidationResult(
            MediaScopeCatalogRevalidationOutcome.STALE_OR_UNAVAILABLE
        )

        assert set(result.__dataclass_fields__) == {"outcome"}
        assert result.outcome is MediaScopeCatalogRevalidationOutcome.STALE_OR_UNAVAILABLE

    def test_port_is_satisfied_structurally(self) -> None:
        class Reader:
            def coverage(
                self, request: MediaScopeCatalogCoverageRequest
            ) -> MediaScopeCatalogCoverageResult:
                _ = request
                return MediaScopeCatalogCoverageResult((WINDOW,))

            def list_scopes(
                self, request: MediaScopeCatalogListRequest
            ) -> MediaScopeCatalogListResult:
                _ = request
                return MediaScopeCatalogListResult(())

            def revalidate(
                self, request: MediaScopeCatalogRevalidationRequest
            ) -> MediaScopeCatalogRevalidationResult:
                _ = request
                return MediaScopeCatalogRevalidationResult(
                    MediaScopeCatalogRevalidationOutcome.CURRENT
                )

        assert isinstance(Reader(), MediaScopeCatalogPort)


def test_list_result_rejects_distinct_scopes_with_one_safe_display_signature() -> None:
    duplicate_label = MediaScopeCatalogCandidate(
        scope=MediaScope(
            commerce_scope=COMMERCE_SCOPE,
            platform=PlatformKey("fictional-other-platform"),
            channel=ChannelKey("fictional-other-channel"),
            marketplace=MarketplaceKey("fictional-marketplace"),
        ),
        safe_signature=SafeMediaScopeDisplaySignature("Fictional media scope"),
        catalog_identity="fictional-other-catalog-identity",
    )

    with pytest.raises(ValueError, match="safe display signature"):
        MediaScopeCatalogListResult((_candidate(), duplicate_label))
