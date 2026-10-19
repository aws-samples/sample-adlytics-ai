# SPDX-License-Identifier: MIT-0
"""Shared-kernel provenance vocabulary: measurement basis, freshness, suppression.

These types are the shared kernel, so their surface is depended on by ports, the
response envelope and the narration layer at once. Two things are pinned here: the
one invariant that makes an unlabelled suppressed result unconstructible, and the
serialised value of every enum member — because those strings are written into
curated rows and read back out, so renaming one silently breaks stored data rather
than failing a build.
"""

from dataclasses import FrozenInstanceError

import pytest

from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression

pytestmark = [pytest.mark.unit]


@pytest.mark.covers("ADR-002")
class TestMeasurementBasis:
    def test_values_are_the_canonical_strings(self) -> None:
        """These reach dim_measurement_basis, so a rename is a data migration."""
        assert MeasurementBasis.AD_ATTRIBUTED == "ad_attributed"
        assert MeasurementBasis.RETAIL_ACTUAL == "retail_actual"
        assert MeasurementBasis.MODELLED == "modelled"

    def test_the_three_bases_are_distinct(self) -> None:
        """Two bases collapsing into one is how ad and retail figures get summed."""
        assert len(set(MeasurementBasis)) == 3


class TestFreshnessStatus:
    def test_values_are_the_canonical_strings(self) -> None:
        assert FreshnessStatus.FRESH == "fresh"
        assert FreshnessStatus.STALE == "stale"
        assert FreshnessStatus.QUARANTINED == "quarantined"

    def test_quarantined_is_not_stale(self) -> None:
        """Stale means answer with a caveat; quarantined means do not answer."""
        assert FreshnessStatus.QUARANTINED is not FreshnessStatus.STALE


class TestSuppression:
    def test_suppressed_must_state_why(self) -> None:
        # Distinguishing zero from suppressed is impossible downstream otherwise.
        with pytest.raises(ValueError, match="state why"):
            Suppression(rows_suppressed=True)

    def test_suppressed_with_reason_is_valid(self) -> None:
        s = Suppression(rows_suppressed=True, reason="97 users < 100 required")
        assert s.rows_suppressed is True
        assert s.reason == "97 users < 100 required"

    def test_unsuppressed_needs_no_reason(self) -> None:
        s = Suppression(rows_suppressed=False)
        assert s.rows_suppressed is False
        assert s.reason is None

    def test_is_immutable(self) -> None:
        """A result that could be relabelled after construction is not a guarantee."""
        s = Suppression(rows_suppressed=True, reason="withheld")
        with pytest.raises(FrozenInstanceError):
            s.rows_suppressed = False  # type: ignore[misc]
