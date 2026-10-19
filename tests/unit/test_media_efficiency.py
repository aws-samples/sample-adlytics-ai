# SPDX-License-Identifier: MIT-0
"""Media Efficiency domain reduction invariants."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from domain.presentation.media_efficiency import (
    AudienceAvailability,
    MediaAudienceValue,
    MediaDatedRow,
    MediaMetric,
    MediaOutcome,
    insufficient_media_efficiency,
    reduce_media_efficiency,
)

pytestmark = pytest.mark.unit


def metric(slot: str, value: str, unit: str = "count") -> MediaMetric:
    return MediaMetric(slot, Decimal(value), unit, "advertising_delivery", "day", "dsp")


def test_reducer_computes_server_ratios_and_keeps_dsp_audience_unavailable() -> None:
    artifact = reduce_media_efficiency(
        artifact_revision="r" * 43,
        parent_list_revision="p" * 43,
        safe_scope_signature="DSP display · US",
        temporal={"resolved": "server"},
        expires_at=datetime(2026, 8, 30, tzinfo=UTC),
        delivery_operands=(
            metric("ad_spend", "20", "USD"),
            metric("impressions", "100"),
            metric("clicks", "4"),
        ),
        delivery_series=(
            MediaDatedRow(
                date(2026, 8, 1),
                (
                    metric("ad_spend", "20", "USD"),
                    metric("impressions", "100"),
                    metric("clicks", "4"),
                    metric("ctr", "0.04", "ratio"),
                    metric("cpc", "5", "USD"),
                    metric("cpm", "200", "USD"),
                ),
            ),
        ),
        viewability_operands=(
            metric("measurable_impressions", "100"),
            metric("viewable_impressions", "70"),
            metric("viewable_impressions_estimated", "80"),
        ),
        viewability_series=(
            MediaDatedRow(
                date(2026, 8, 1),
                (
                    metric("measurable_impressions", "100"),
                    metric("viewable_impressions", "70"),
                    metric("viewable_impressions_estimated", "80"),
                    metric("viewable_rate", "0.7", "ratio"),
                ),
            ),
        ),
        outcome=MediaOutcome.COMPLETE,
    )

    assert artifact.delivery_summary[3].slot == "ctr"
    assert artifact.delivery_summary[3].value == Decimal("0.04")
    assert artifact.viewability_summary[-1].value == Decimal("0.7")
    assert all(
        value.availability is AudienceAvailability.UNAVAILABLE for value in artifact.audience
    )
    assert all(value.value is None for value in artifact.audience)


def test_unavailable_audience_rejects_a_numeric_value() -> None:
    with pytest.raises(ValueError, match="unavailable audience"):
        MediaAudienceValue(
            "reach_users",
            AudienceAvailability.UNAVAILABLE,
            Decimal("1"),
            None,
            "exact_source_audience_metric_unavailable",
            "contact_workspace_data_owner",
        )


def test_reducer_rejects_nonreconciling_delivery_series() -> None:
    with pytest.raises(ValueError, match="does not reconcile"):
        reduce_media_efficiency(
            artifact_revision="r" * 43,
            parent_list_revision="p" * 43,
            safe_scope_signature="DSP display · US",
            temporal={"resolved": "server"},
            expires_at=datetime(2026, 8, 30, tzinfo=UTC),
            delivery_operands=(
                metric("ad_spend", "20", "USD"),
                metric("impressions", "100"),
                metric("clicks", "4"),
            ),
            delivery_series=(
                MediaDatedRow(
                    date(2026, 8, 1),
                    (
                        metric("ad_spend", "19", "USD"),
                        metric("impressions", "100"),
                        metric("clicks", "4"),
                        metric("ctr", "0.04", "ratio"),
                        metric("cpc", "4.75", "USD"),
                        metric("cpm", "190", "USD"),
                    ),
                ),
            ),
            viewability_operands=(
                metric("measurable_impressions", "100"),
                metric("viewable_impressions", "70"),
                metric("viewable_impressions_estimated", "80"),
            ),
            viewability_series=(
                MediaDatedRow(
                    date(2026, 8, 1),
                    (
                        metric("measurable_impressions", "100"),
                        metric("viewable_impressions", "70"),
                        metric("viewable_impressions_estimated", "80"),
                        metric("viewable_rate", "0.7", "ratio"),
                    ),
                ),
            ),
        )


def test_insufficient_artifact_cannot_carry_fabricated_rows_or_summaries() -> None:
    artifact = insufficient_media_efficiency(
        artifact_revision="r" * 43,
        parent_list_revision="p" * 43,
        safe_scope_signature="DSP display · US",
        temporal={"resolved": "server"},
        expires_at=datetime(2026, 8, 30, tzinfo=UTC),
        reason_codes=("no_exact_current_media_evidence",),
    )

    assert artifact.outcome is MediaOutcome.INSUFFICIENT
    assert artifact.delivery_summary == ()
    assert artifact.viewability_series == ()
    assert artifact.visuals == ()
