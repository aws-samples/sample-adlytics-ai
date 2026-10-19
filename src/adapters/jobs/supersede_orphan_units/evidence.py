# SPDX-License-Identifier: MIT-0
"""TD-50: decide, from durable authority alone, whether an orphan plan unit is superseded.

Pure: every input is an item or object already read; nothing here reads or writes AWS.

An **orphan plan** has `META` `planned` with full admission and no failure; its origin intent is
`admitted`, or (gate 1 only) absent, so no reconciler owns it; and each unit is either
`attested` or an **orphan unit**: `queued`, `admitted`, never begun (no attempt reference,
reservation or bound attempt).
Its reservations pass one of two plan-shape gates (`PlanShape`):

- gate 1, `no_reservation`: the `META` carries no `plan_reservation_count`, and no attested
  sibling carries a `unit_reservation_count` (the zero sum);
- gate 2, `reservations_accounted_for` (FEAT-011): the `META` `plan_reservation_count` equals the
  sum of the spent units' `unit_reservation_count` (attested siblings, plus the one accounted
  failed unit under the variant below), and every spent unit carries a present count from 1 to
  the permanent per-unit cap `MAX_ACCEPTED_ATTEMPTS_PER_UNIT` (10) (a spent unit passed Begin,
  so an absent count is a torn shape, never read as zero, and Begin refuses at the cap, so a
  count above it is a shape no Begin sequence writes). The origin
  intent must be present and `admitted`; gate 1's allowance for an absent intent does not
  extend to gate 2 or the variant. The identity is exact, not a heuristic: Begin
  (`DynamoProjectionCoordinator._begin_queued_owner_members`) is the only writer of either
  counter, and it adds one to the unit's and the plan's counter in one transaction; plan and
  unit creation put neither. So the `META` counter always equals the sum over every unit. Every
  other unit here is never begun (it carries no counter), so the spent units' sum is the whole
  sum. A reservation on any other shape (a begun queued unit, a writing or bound unit, an
  unaccounted failed unit) fails the unit-shape or plan-shape gate before the sum is compared,
  and a sum that still differs is refused as `plan_reservations_not_accounted_for`.

The **failed-unit variant** (`failed_unit_projected_past`, FEAT-011 class b) admits a plan with
exactly one `failed` unit, only when the operator names that plan and accounts for its dead-letter
message, and only when every object of the failed unit is proven by the projection rule below.
The failed unit is never rewritten; the plan `META` alone becomes `superseded`.

Anything else is not an orphan and is left untouched.

An orphan unit is **superseded** only when every one of its objects is proven, by one rule each:

- a canonical object (`current` or `candidate`): its partition's `CURRENT` projection is at or past
  the object's publication. That is `projected_fencing_token > token`, or equal to `token` with the
  same projected publication. An equal token naming another publication is not proof;
- a source object (`source_aligned`): a newer attested append. An `attested` unit of another plan,
  on the same physical table, carries a source publication with the same delivery scope and
  interpreter, a coverage window that contains this one, and a strictly later `published_at`.

A plan is dispositioned only when every orphan unit in it is proven; otherwise none of its units
is touched and each unproven one is reported with its reason.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from domain.operations.projection_feasibility import MAX_ACCEPTED_ATTEMPTS_PER_UNIT
from domain.operations.status import canonical_authority_digest
from domain.operations.write_plan import (
    MAX_PLAN_ATTEMPTS,
    IntentState,
    PlanStatus,
    PlanUnitAdmission,
    PlanUnitStatus,
)

if TYPE_CHECKING:
    from datetime import datetime

    from domain.semantics.source_aligned import SourceAlignedPublication

__all__ = [
    "EVIDENCE_NAMESPACE",
    "OrphanPlan",
    "PlanRows",
    "PlanShape",
    "Rule",
    "SourceFacts",
    "Unproven",
    "evidence_digest",
    "never_begun",
    "orphan_plan",
    "orphan_units",
    "projection_evidence",
    "source_evidence",
    "source_facts",
    "unit_reservations",
]

EVIDENCE_NAMESPACE: Final = "stage1-orphan-unit-supersession/v1"
_NEVER_BEGUN_FIELDS: Final = (
    "attempt_references",
    "unit_reservation_count",
    "current_attempt_id",
    "terminal_at",
)
_NONE_PROJECTED: Final = frozenset({None, "", "NONE"})

type Plain = Mapping[str, object]


class Rule(StrEnum):
    """The two supersession rules (TD-50; integration step-1 report §6, option 1)."""

    PROJECTION_AT_OR_PAST = "projection_at_or_past"
    NEWER_ATTESTED_SOURCE_APPEND = "newer_attested_source_append"


class Unproven(StrEnum):
    """Why a plan or unit is reported and left untouched."""

    NOT_PLANNED = "plan_not_planned"
    PLAN_SHAPE = "plan_counters_not_orphan"
    INTENT_LIVE = "origin_intent_live"
    INTENT_ABSENT = "origin_intent_absent"
    UNIT_SHAPE = "unit_not_orphan_shape"
    NO_ORPHAN_UNIT = "no_orphan_unit"
    SIBLING_UNPROVEN = "plan_has_an_unproven_unit"
    CURRENT_ABSENT = "current_absent"
    PROJECTION_ABSENT = "projection_absent"
    PROJECTION_BEHIND = "projection_behind_publication"
    PROJECTION_OTHER_PUBLICATION = "projection_names_another_publication_at_its_token"
    OBJECT_UNREADABLE = "immutable_object_unreadable"
    OBJECT_DISAGREES = "immutable_object_disagrees_with_unit"
    NO_NEWER_APPEND = "no_newer_attested_source_append"
    UNKNOWN_KIND = "unit_object_kind_unknown"
    RESERVATIONS_UNACCOUNTED = "plan_reservations_not_accounted_for"
    FAILED_UNIT_UNACCOUNTED = "failed_unit_dead_letter_not_accounted_for"


class PlanShape(StrEnum):
    """Which plan-shape gate admitted an orphan plan (module docstring)."""

    NO_RESERVATION = "no_reservation"
    RESERVATIONS_ACCOUNTED_FOR = "reservations_accounted_for"
    FAILED_UNIT_PROJECTED_PAST = "failed_unit_projected_past"


@dataclass(frozen=True, slots=True)
class PlanRows:
    """One plan partition as read: its `META`, its units by unit key, and its origin intent."""

    plan_key: str
    meta: Plain
    units: Mapping[str, Plain]
    intent: Plain | None


@dataclass(frozen=True, slots=True)
class OrphanPlan:
    """An orphan plan as its plan-shape gate admitted it."""

    shape: PlanShape
    orphans: tuple[Plain, ...]
    """The never-begun units to prove and supersede, in unit-key order (empty only for b)."""
    failed: Plain | None
    """The one accounted failed unit (variant b); it is condition-checked, never rewritten."""
    reservations: int | None
    """The `META` `plan_reservation_count` the transaction pins; None under gate 1 (absent)."""


def _int(item: Plain, name: str) -> int | None:
    value = item.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def never_begun(unit: Plain) -> bool:
    """Say whether a unit is `queued`, admitted, with no attempt reference, reservation or bind."""
    return (
        unit.get("status") == PlanUnitStatus.QUEUED.value
        and unit.get("admission") == PlanUnitAdmission.ADMITTED.value
        and all(name not in unit for name in _NEVER_BEGUN_FIELDS)
    )


def unit_reservations(unit: Plain) -> int | None:
    """Return a unit's permanent reservation count; absent is 0.

    A present value that is not a plain integer from 1 to `MAX_ACCEPTED_ATTEMPTS_PER_UNIT` is
    None: Begin writes 1 on the first reservation and refuses at the cap, so 0, a negative value
    or a value above the cap is a shape no Begin sequence writes.
    """
    if "unit_reservation_count" not in unit:
        return 0
    count = _int(unit, "unit_reservation_count")
    return count if count is not None and 1 <= count <= MAX_ACCEPTED_ATTEMPTS_PER_UNIT else None


def _accounted_reservations(meta: Plain, spent: tuple[Plain, ...]) -> int | None:
    """Gate 2: the plan counter, only when it equals the spent units' sum exactly.

    Every spent unit must carry a present `unit_reservation_count` from 1 to the permanent
    per-unit cap. A spent unit (attested, or the accounted failed one) passed Begin, which writes
    its counter and the plan's together, so an absent count is a torn shape outside the identity,
    not a zero; and Begin refuses at the cap (`TableWriter` `begin_owner`), so a count above it
    did not come from Begin and the equality would prove nothing about it.
    """
    plan_count = _int(meta, "plan_reservation_count")
    if plan_count is None or not 1 <= plan_count <= MAX_PLAN_ATTEMPTS:
        return None
    total = 0
    for unit in spent:
        if "unit_reservation_count" not in unit:
            return None
        count = unit_reservations(unit)
        if count is None:
            return None
        total += count
    return plan_count if total == plan_count else None


def _settled_failure(unit: Plain) -> bool:
    """Say whether a unit is failed, admitted, binds no attempt and spent a reservation."""
    return (
        unit.get("status") == PlanUnitStatus.FAILED.value
        and unit.get("admission") == PlanUnitAdmission.ADMITTED.value
        and "current_attempt_id" not in unit
        and (_int(unit, "unit_reservation_count") or 0) >= 1
    )


def orphan_plan(  # noqa: PLR0911, PLR0912 -- one typed refusal per gate, in order
    plan: PlanRows, *, failed_unit_accounted: bool = False
) -> tuple[OrphanPlan | None, Unproven | None]:
    """Return the orphan plan as a gate admits it, or the reason it is not an orphan plan.

    `failed_unit_accounted` is true only for the one plan whose failed unit's dead-letter message
    the operator has accounted for (`--failed-unit-plan`); every other plan must have no failure.
    """
    meta = plan.meta
    if meta.get("status") != PlanStatus.PLANNED.value or "terminal_at" in meta:
        return None, Unproven.NOT_PLANNED
    count = _int(meta, "unit_count")
    ordered = [unit for _, unit in sorted(plan.units.items())]
    attested = tuple(u for u in ordered if u.get("status") == PlanUnitStatus.ATTESTED.value)
    failed = tuple(u for u in ordered if u.get("status") == PlanUnitStatus.FAILED.value)
    expected_failed = 1 if failed_unit_accounted else 0
    if (
        count is None
        or count != len(plan.units)
        or _int(meta, "admitted_count") != count
        or _int(meta, "failed_count") != expected_failed
        or _int(meta, "attested_count") != len(attested)
    ):
        return None, Unproven.PLAN_SHAPE
    if len(failed) != expected_failed:
        return None, Unproven.PLAN_SHAPE
    if any(not _settled_failure(unit) for unit in failed):
        return None, Unproven.UNIT_SHAPE
    reservations: int | None = None
    if "plan_reservation_count" in meta:
        reservations = _accounted_reservations(meta, (*attested, *failed))
        if reservations is None:
            return None, Unproven.RESERVATIONS_UNACCOUNTED
    elif failed or any(unit_reservations(unit) != 0 for unit in attested):
        # Gate 1 is the zero sum: a spent unit reservation with no plan counter breaks the
        # identity, so the plan's state is not what Begin writes and it is left untouched.
        return None, Unproven.RESERVATIONS_UNACCOUNTED
    if plan.intent is not None and plan.intent.get("state") != IntentState.ADMITTED.value:
        return None, Unproven.INTENT_LIVE
    spent_statuses = {PlanUnitStatus.ATTESTED.value, PlanUnitStatus.FAILED.value}
    orphans = tuple(unit for unit in ordered if unit.get("status") not in spent_statuses)
    if any(not never_begun(unit) for unit in orphans):
        return None, Unproven.UNIT_SHAPE
    if not orphans and not failed:
        return None, Unproven.NO_ORPHAN_UNIT
    if failed:
        shape = PlanShape.FAILED_UNIT_PROJECTED_PAST
    elif reservations is not None:
        shape = PlanShape.RESERVATIONS_ACCOUNTED_FOR
    else:
        shape = PlanShape.NO_RESERVATION
    if shape is not PlanShape.NO_RESERVATION and plan.intent is None:
        # Gate 2 and the variant terminalize a plan whose units spent reservations; that needs
        # the admitted intent as evidence, so its absence refuses (gate 1 keeps its rule).
        return None, Unproven.INTENT_ABSENT
    return OrphanPlan(shape, orphans, failed[0] if failed else None, reservations), None


def orphan_units(plan: PlanRows) -> tuple[tuple[Plain, ...], Unproven | None]:
    """Return the plan's orphan units, or the reason it is not an orphan plan (no failure)."""
    admitted, reason = orphan_plan(plan)
    return ((), reason) if admitted is None else (admitted.orphans, None)


def projection_evidence(  # noqa: PLR0913, PLR0917 -- one publication's identity and its pointer
    partition: str, publication_id: str, fencing_token: int, current: Plain | None
) -> tuple[dict[str, object] | None, Unproven | None]:
    """Rule 1: the partition's `CURRENT` projection is at or past this publication."""
    if current is None:
        return None, Unproven.CURRENT_ABSENT
    projected = current.get("projected_publication_id")
    token = _int(current, "projected_fencing_token")
    if projected in _NONE_PROJECTED or token is None:
        return None, Unproven.PROJECTION_ABSENT
    if token < fencing_token:
        return None, Unproven.PROJECTION_BEHIND
    if token == fencing_token and projected != publication_id:
        return None, Unproven.PROJECTION_OTHER_PUBLICATION
    return {
        "partition": partition,
        "publication_id": publication_id,
        "fencing_token": fencing_token,
        "projected_publication_id": str(projected),
        "projected_fencing_token": token,
    }, None


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """The facts of one immutable source publication rule 2 compares."""

    reference: str
    plan_key: str
    unit_key: str
    scope: str
    """The delivery scope and interpreter, as canonical JSON."""
    coverage_start: str
    coverage_end: str
    published_at: datetime


def source_facts(
    publication: SourceAlignedPublication, plan_key: str, unit_key: str
) -> SourceFacts:
    """Extract the compared facts from one decoded source publication."""
    rowset = publication.rowset
    scope = rowset.delivery_scope
    window = rowset.extraction_coverage.window
    return SourceFacts(
        reference=publication.reference.value,
        plan_key=plan_key,
        unit_key=unit_key,
        scope=json.dumps(
            {
                "source_system": scope.source_system.value,
                "source_dataset": scope.source_dataset.value,
                "extraction_profile": scope.extraction_profile.value,
                "extraction_profile_version": scope.extraction_profile.version,
                "source_scope_namespace": scope.source_scope.namespace,
                "source_scope_value": scope.source_scope.value,
                "option_set_hash": scope.option_set_hash,
                "interpreter": rowset.interpreter.value,
                "interpreter_version": rowset.interpreter.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        coverage_start=window.start.isoformat(),
        coverage_end=window.end.isoformat(),
        published_at=publication.published_at,
    )


def _facts(facts: SourceFacts) -> dict[str, object]:
    return {
        "reference": facts.reference,
        "plan_key": facts.plan_key,
        "unit_key": facts.unit_key,
        "scope": json.loads(facts.scope),
        "coverage": [facts.coverage_start, facts.coverage_end],
        "published_at": facts.published_at.isoformat(),
    }


def source_evidence(
    orphan: SourceFacts, attested: Sequence[SourceFacts]
) -> tuple[dict[str, object] | None, Unproven | None]:
    """Rule 2: a newer attested append on the same table covers this source publication."""
    newer = [
        candidate
        for candidate in attested
        if candidate.unit_key == orphan.unit_key
        and candidate.plan_key != orphan.plan_key
        and candidate.scope == orphan.scope
        and candidate.coverage_start <= orphan.coverage_start
        and candidate.coverage_end >= orphan.coverage_end
        and candidate.published_at > orphan.published_at
    ]
    if not newer:
        return None, Unproven.NO_NEWER_APPEND
    chosen = max(newer, key=lambda candidate: (candidate.published_at, candidate.reference))
    return {"orphan": _facts(orphan), "newer_attested_append": _facts(chosen)}, None


def evidence_digest(record: Mapping[str, object]) -> str:
    """Hash one evidence record under the disposition's namespace (64 lowercase hex)."""
    return canonical_authority_digest([EVIDENCE_NAMESPACE, dict(record)])
