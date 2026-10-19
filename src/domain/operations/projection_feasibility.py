# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 §2.1 projection feasibility over the canonical roster.

The clean-attempt formula is exactly
``clean_attempts = Σ(u in complete admitted roster)(M(u) + 1) <= 32`` with every
``M(u)+1 <= 10``; the ``+1`` is exactly one final verification per unit. The sum
covers the complete roster that will be persisted, is evaluated before any
authority mutation, and an empty roster follows the existing no-plan path and
contributes zero. ``M(u)`` comes only from the single §6 existing ordering
helper/packer — the adapter measures it and supplies a ``UnitMergeMeasurement``
per unit; a preflight-only packer implementation is forbidden, so this module
never re-derives a merge count. The roster's stored deterministic ``merge_count``
must agree byte-for-byte with the measured count or the delivery refuses.

Deterministic refusal, not exception: an infeasible delivery returns the typed
refusal before any authority mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from domain.operations.write_plan import (
    MAX_PLAN_ATTEMPTS,
    CanonicalUnitEntry,
    WritePlanError,
    intent_roster_tuple,
)

__all__ = [
    "MAX_ACCEPTED_ATTEMPTS_PER_PLAN",
    "MAX_ACCEPTED_ATTEMPTS_PER_UNIT",
    "FeasibilityRefusalReason",
    "FeasibleProjection",
    "ProjectionFeasibilityError",
    "ProjectionFeasibilityRefusal",
    "ProjectionFeasibilityRequest",
    "ProjectionFeasibilityResult",
    "UnitMergeMeasurement",
    "validate_projection_feasibility",
]

# §11 reviewed values: maxAcceptedAttemptsPerUnit and maxAcceptedAttemptsPerPlan.
MAX_ACCEPTED_ATTEMPTS_PER_UNIT: Final = 10
MAX_ACCEPTED_ATTEMPTS_PER_PLAN: Final = MAX_PLAN_ATTEMPTS


class ProjectionFeasibilityError(ValueError):
    """A feasibility input violates its closed form before any decision is made."""


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class UnitMergeMeasurement:
    """One unit's deterministic ``M(u)``, measured by the single §6 existing packer."""

    unit_key: str
    merge_count: int

    def __post_init__(self) -> None:
        if not self.unit_key.strip():
            raise ProjectionFeasibilityError("a merge measurement names its exact unit key.")
        if not _is_plain_int(self.merge_count) or self.merge_count < 1:
            raise ProjectionFeasibilityError(
                "a unit with at least one object always merges at least once."
            )


@dataclass(frozen=True, slots=True)
class ProjectionFeasibilityRequest:
    """The complete candidate roster beside its per-unit packer measurements.

    The attempt caps default to the §11 reviewed values; an entry point resolving
    ``AppConfig.ingestion`` passes the same values rather than new ones.
    """

    units: tuple[CanonicalUnitEntry, ...]
    measurements: tuple[UnitMergeMeasurement, ...]
    max_accepted_attempts_per_unit: int = MAX_ACCEPTED_ATTEMPTS_PER_UNIT
    max_accepted_attempts_per_plan: int = MAX_ACCEPTED_ATTEMPTS_PER_PLAN

    def __post_init__(self) -> None:
        for name in ("max_accepted_attempts_per_unit", "max_accepted_attempts_per_plan"):
            value: int = getattr(self, name)
            if not _is_plain_int(value) or value < 1:
                raise ProjectionFeasibilityError(f"{name} must be a positive integer.")


class FeasibilityRefusalReason(StrEnum):
    """The closed feasibility refusal vocabulary."""

    ROSTER_FORM = "roster_outside_canonical_form"
    MEASUREMENT_MISMATCH = "measurements_do_not_cover_the_exact_roster"
    MERGE_COUNT_DISAGREES = "roster_merge_count_disagrees_with_packer"
    UNIT_ATTEMPTS_EXCEEDED = "unit_clean_attempts_exceed_cap"
    PLAN_ATTEMPTS_EXCEEDED = "plan_clean_attempts_exceed_cap"


@dataclass(frozen=True, slots=True)
class ProjectionFeasibilityRefusal:
    """The deterministic pre-authority refusal; nothing has been mutated."""

    reason: FeasibilityRefusalReason
    description: str


@dataclass(frozen=True, slots=True)
class FeasibleProjection:
    """A proved-feasible roster: its clean-attempt total and per-unit terms.

    ``unit_clean_attempts`` pairs each unit key with its ``M(u)+1`` term in roster
    order; ``creates_plan`` is ``False`` exactly for the empty roster, which
    follows the existing no-plan path.
    """

    clean_attempts: int
    unit_clean_attempts: tuple[tuple[str, int], ...]
    creates_plan: bool


type ProjectionFeasibilityResult = FeasibleProjection | ProjectionFeasibilityRefusal


def validate_projection_feasibility(  # noqa: PLR0911 -- one typed return per closed refusal
    request: ProjectionFeasibilityRequest,
) -> ProjectionFeasibilityResult:
    """Prove the §2.1 clean-attempt formula over the complete candidate roster.

    Returns the typed feasible result or the deterministic typed refusal; this
    function mutates nothing and reads no clock, config, or store.
    """
    if not request.units:
        if request.measurements:
            return ProjectionFeasibilityRefusal(
                FeasibilityRefusalReason.MEASUREMENT_MISMATCH,
                "an empty roster carries no merge measurements.",
            )
        return FeasibleProjection(clean_attempts=0, unit_clean_attempts=(), creates_plan=False)
    try:
        intent_roster_tuple(request.units)
    except WritePlanError as error:
        return ProjectionFeasibilityRefusal(FeasibilityRefusalReason.ROSTER_FORM, str(error))
    if len(request.measurements) != len(request.units) or any(
        measurement.unit_key != unit.unit_key
        for measurement, unit in zip(request.measurements, request.units, strict=True)
    ):
        return ProjectionFeasibilityRefusal(
            FeasibilityRefusalReason.MEASUREMENT_MISMATCH,
            "measurements must cover exactly every roster unit, in roster order.",
        )
    terms: list[tuple[str, int]] = []
    for unit, measurement in zip(request.units, request.measurements, strict=True):
        if unit.merge_count != measurement.merge_count:
            return ProjectionFeasibilityRefusal(
                FeasibilityRefusalReason.MERGE_COUNT_DISAGREES,
                f"unit {unit.unit_key!r} stores merge count {unit.merge_count} but the "
                f"packer measured {measurement.merge_count}.",
            )
        term = measurement.merge_count + 1
        if term > request.max_accepted_attempts_per_unit:
            return ProjectionFeasibilityRefusal(
                FeasibilityRefusalReason.UNIT_ATTEMPTS_EXCEEDED,
                f"unit {unit.unit_key!r} needs {term} clean attempts, beyond the "
                f"{request.max_accepted_attempts_per_unit}-attempt unit cap.",
            )
        terms.append((unit.unit_key, term))
    clean_attempts = sum(term for _, term in terms)
    if clean_attempts > request.max_accepted_attempts_per_plan:
        return ProjectionFeasibilityRefusal(
            FeasibilityRefusalReason.PLAN_ATTEMPTS_EXCEEDED,
            f"the complete roster needs {clean_attempts} clean attempts, beyond the "
            f"{request.max_accepted_attempts_per_plan}-attempt plan cap.",
        )
    return FeasibleProjection(
        clean_attempts=clean_attempts,
        unit_clean_attempts=tuple(terms),
        creates_plan=True,
    )
