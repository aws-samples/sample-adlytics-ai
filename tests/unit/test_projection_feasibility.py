# SPDX-License-Identifier: MIT-0
"""ADR-075 §2.1 projection feasibility: the clean-attempt formula over the roster."""

from __future__ import annotations

import pytest

from domain.operations.projection_feasibility import (
    FeasibilityRefusalReason,
    FeasibleProjection,
    ProjectionFeasibilityError,
    ProjectionFeasibilityRefusal,
    ProjectionFeasibilityRequest,
    UnitMergeMeasurement,
    validate_projection_feasibility,
)
from domain.operations.write_plan import CanonicalObjectEntry, CanonicalUnitEntry

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.
CHECKSUM = "d" * 64


def _unit(unit_key: str, merge_count: int) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=unit_key,
        table_kind="current",
        merge_count=merge_count,
        objects=(
            CanonicalObjectEntry(
                kind="canonical",
                reference=f"obj-{unit_key}",
                content_checksum=CHECKSUM,
                publication_id="pub-fixture-0001",
                partition_key=None,
                fencing_token=7,
            ),
        ),
    )


def _measured(*pairs: tuple[str, int]) -> tuple[UnitMergeMeasurement, ...]:
    return tuple(UnitMergeMeasurement(unit_key=key, merge_count=count) for key, count in pairs)


def test_an_empty_roster_is_feasible_with_zero_clean_attempts_and_no_plan() -> None:
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(units=(), measurements=())
    )
    assert isinstance(result, FeasibleProjection)
    assert result.clean_attempts == 0
    assert result.unit_clean_attempts == ()
    assert result.creates_plan is False


def test_an_empty_roster_with_a_leftover_measurement_refuses() -> None:
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(units=(), measurements=_measured(("unit-a", 1)))
    )
    assert isinstance(result, ProjectionFeasibilityRefusal)
    assert result.reason is FeasibilityRefusalReason.MEASUREMENT_MISMATCH


def test_a_single_unit_with_two_measured_merges_needs_three_clean_attempts() -> None:
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=(_unit("unit-a", 2),), measurements=_measured(("unit-a", 2))
        )
    )
    assert isinstance(result, FeasibleProjection)
    assert result.clean_attempts == 3
    assert result.unit_clean_attempts == (("unit-a", 3),)
    assert result.creates_plan is True


def test_a_roster_merge_count_that_disagrees_with_the_packer_measurement_refuses() -> None:
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=(_unit("unit-a", 2),), measurements=_measured(("unit-a", 3))
        )
    )
    assert isinstance(result, ProjectionFeasibilityRefusal)
    assert result.reason is FeasibilityRefusalReason.MERGE_COUNT_DISAGREES


def test_measurements_that_do_not_cover_every_unit_in_roster_order_refuse() -> None:
    units = (_unit("unit-a", 1), _unit("unit-b", 1))
    missing = validate_projection_feasibility(
        ProjectionFeasibilityRequest(units=units, measurements=_measured(("unit-a", 1)))
    )
    assert isinstance(missing, ProjectionFeasibilityRefusal)
    assert missing.reason is FeasibilityRefusalReason.MEASUREMENT_MISMATCH
    swapped = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=units, measurements=_measured(("unit-b", 1), ("unit-a", 1))
        )
    )
    assert isinstance(swapped, ProjectionFeasibilityRefusal)
    assert swapped.reason is FeasibilityRefusalReason.MEASUREMENT_MISMATCH


def test_nine_merges_plus_one_verification_fill_the_unit_cap_exactly() -> None:
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=(_unit("unit-a", 9),), measurements=_measured(("unit-a", 9))
        )
    )
    assert isinstance(result, FeasibleProjection)
    assert result.unit_clean_attempts == (("unit-a", 10),)


def test_ten_merges_exceed_the_ten_attempt_unit_cap_and_refuse() -> None:
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=(_unit("unit-a", 10),), measurements=_measured(("unit-a", 10))
        )
    )
    assert isinstance(result, ProjectionFeasibilityRefusal)
    assert result.reason is FeasibilityRefusalReason.UNIT_ATTEMPTS_EXCEEDED


def test_a_complete_roster_summing_to_exactly_32_clean_attempts_is_feasible() -> None:
    units = tuple(_unit(f"unit-{tag}", 7) for tag in "abcd")
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=units, measurements=_measured(*((unit.unit_key, 7) for unit in units))
        )
    )
    assert isinstance(result, FeasibleProjection)
    assert result.clean_attempts == 32


def test_a_complete_roster_summing_to_33_clean_attempts_refuses_before_authority() -> None:
    """The sum is over the complete roster, so it refuses even when every unit fits alone."""
    units = (_unit("unit-a", 7), _unit("unit-b", 7), _unit("unit-c", 7), _unit("unit-d", 8))
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=units,
            measurements=_measured(*((unit.unit_key, unit.merge_count) for unit in units)),
        )
    )
    assert isinstance(result, ProjectionFeasibilityRefusal)
    assert result.reason is FeasibilityRefusalReason.PLAN_ATTEMPTS_EXCEEDED


def test_a_sixteen_unit_roster_is_outside_the_canonical_form() -> None:
    units = tuple(_unit(f"unit-{index:02d}", 1) for index in range(16))
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=units, measurements=_measured(*((unit.unit_key, 1) for unit in units))
        )
    )
    assert isinstance(result, ProjectionFeasibilityRefusal)
    assert result.reason is FeasibilityRefusalReason.ROSTER_FORM


def test_an_unsorted_roster_is_outside_the_canonical_form() -> None:
    units = (_unit("unit-b", 1), _unit("unit-a", 1))
    result = validate_projection_feasibility(
        ProjectionFeasibilityRequest(
            units=units, measurements=_measured(("unit-b", 1), ("unit-a", 1))
        )
    )
    assert isinstance(result, ProjectionFeasibilityRefusal)
    assert result.reason is FeasibilityRefusalReason.ROSTER_FORM


def test_a_measurement_below_one_merge_is_unconstructible() -> None:
    with pytest.raises(ProjectionFeasibilityError):
        UnitMergeMeasurement(unit_key="unit-a", merge_count=0)
    with pytest.raises(ProjectionFeasibilityError):
        UnitMergeMeasurement(unit_key="   ", merge_count=1)


def test_request_caps_must_be_positive_plain_integers() -> None:
    with pytest.raises(ProjectionFeasibilityError):
        ProjectionFeasibilityRequest(units=(), measurements=(), max_accepted_attempts_per_unit=0)
    with pytest.raises(ProjectionFeasibilityError):
        ProjectionFeasibilityRequest(
            units=(),
            measurements=(),
            max_accepted_attempts_per_plan=True,  # type: ignore[arg-type]
        )
