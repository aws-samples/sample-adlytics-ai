# SPDX-License-Identifier: MIT-0
"""Complete immutable SCD2 history construction for product and campaign dimensions."""

from datetime import UTC, datetime

import pytest

from domain.semantics.canonical_compiler import (
    CanonicalRow,
    canonical_scalar,
)
from domain.semantics.dimension_history import (
    OPEN_ENDED,
    DimensionHistoryRequest,
    build_dimension_history,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-053")]


def _row(**values: object) -> CanonicalRow:
    return tuple(
        sorted(((name, canonical_scalar(value, column=name)) for name, value in values.items()))
    )


def _request(
    previous: tuple[CanonicalRow, ...],
    observations: tuple[CanonicalRow, ...],
    boundary: datetime,
) -> DimensionHistoryRequest:
    return DimensionHistoryRequest(
        previous=previous,
        observations=observations,
        business_key=("campaign_key",),
        tracked_attributes=("campaign_name", "channel_key"),
        boundary=boundary,
    )


def test_first_observation_opens_one_current_version() -> None:
    boundary = datetime(2026, 8, 10, tzinfo=UTC)
    result = build_dimension_history(
        _request(
            (),
            (_row(campaign_key="campaign:a", campaign_name="First", channel_key="dsp"),),
            boundary,
        )
    )

    row = dict(result.rows[0])
    assert row["valid_from"] == boundary
    assert row["valid_to"] == OPEN_ENDED
    assert row["is_current"] is True


def test_rename_closes_the_prior_version_and_opens_one_successor() -> None:
    first = datetime(2026, 8, 10, tzinfo=UTC)
    renamed = datetime(2026, 8, 12, tzinfo=UTC)
    initial = build_dimension_history(
        _request(
            (),
            (_row(campaign_key="campaign:a", campaign_name="First", channel_key="dsp"),),
            first,
        )
    )
    result = build_dimension_history(
        _request(
            initial.rows,
            (_row(campaign_key="campaign:a", campaign_name="Growth", channel_key="dsp"),),
            renamed,
        )
    )

    rows = tuple(dict(row) for row in result.rows)
    assert len(rows) == 2
    assert rows[0]["valid_to"] == renamed
    assert rows[0]["is_current"] is False
    assert rows[1]["valid_from"] == renamed
    assert rows[1]["campaign_name"] == "Growth"
    assert rows[1]["is_current"] is True


def test_unchanged_and_omitted_identities_preserve_existing_history() -> None:
    first = datetime(2026, 8, 10, tzinfo=UTC)
    later = datetime(2026, 8, 12, tzinfo=UTC)
    initial = build_dimension_history(
        _request(
            (),
            (
                _row(campaign_key="campaign:a", campaign_name="First", channel_key="dsp"),
                _row(campaign_key="campaign:b", campaign_name="Second", channel_key="dsp"),
            ),
            first,
        )
    )
    result = build_dimension_history(
        _request(
            initial.rows,
            (_row(campaign_key="campaign:a", campaign_name="First", channel_key="dsp"),),
            later,
        )
    )

    assert result.rows == initial.rows
