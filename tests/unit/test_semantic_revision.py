# SPDX-License-Identifier: MIT-0
"""Source-neutral revision precision and ordering prerequisites for Semantic V2 state.

These tests intentionally do not claim ADR-047 coverage: that decision is about attempts, leases,
fencing, successful publications and exact interval sets. Revision evidence is one input to that
state model, and its adapters remain later work.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from domain.semantics.revision import (
    RevisionSchemeKey,
    SourceRevision,
    SourceRevisionError,
)

pytestmark = [pytest.mark.unit]

AMC = RevisionSchemeKey("amc.workflow_execution")
VENDOR = RevisionSchemeKey("vendor_central.last_updated_date")


def test_two_instant_revisions_on_one_day_keep_their_order() -> None:
    first = SourceRevision.instant(
        scheme=AMC,
        source_id="execution-1",
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    second = SourceRevision.instant(
        scheme=AMC,
        source_id="execution-2",
        observed_at=datetime(2026, 8, 25, 17, 0, tzinfo=UTC),
    )
    assert second.is_newer_than(first) is True


def test_date_revision_remains_date_precision() -> None:
    first = SourceRevision.dated(
        scheme=VENDOR,
        source_id="2026-08-24",
        observed_on=date(2026, 8, 24),
    )
    second = SourceRevision.dated(
        scheme=VENDOR,
        source_id="2026-08-25",
        observed_on=date(2026, 8, 25),
    )
    assert second.is_newer_than(first) is True
    assert second.source_id == "2026-08-25"


def test_revision_requires_an_aware_instant() -> None:
    naive = datetime(2026, 8, 25, 9, 0, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(SourceRevisionError, match="timezone-aware"):
        SourceRevision.instant(scheme=AMC, source_id="execution-1", observed_at=naive)


def test_different_source_revision_schemes_have_no_shared_order() -> None:
    amc = SourceRevision.instant(
        scheme=AMC,
        source_id="execution-1",
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    vendor = SourceRevision.dated(
        scheme=VENDOR,
        source_id="2026-08-25",
        observed_on=date(2026, 8, 25),
    )
    with pytest.raises(SourceRevisionError, match="different revision vocabularies"):
        amc.compare(vendor)


def test_same_scheme_cannot_mix_date_and_instant_precision() -> None:
    instant = SourceRevision.instant(
        scheme=AMC,
        source_id="execution-1",
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    dated = SourceRevision.dated(
        scheme=AMC,
        source_id="2026-08-25",
        observed_on=date(2026, 8, 25),
    )
    with pytest.raises(SourceRevisionError, match="instant against date"):
        instant.compare(dated)
