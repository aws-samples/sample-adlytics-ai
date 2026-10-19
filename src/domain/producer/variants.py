# SPDX-License-Identifier: MIT-0
"""Which declared pack variant a delivery must be, decided from the previous period (plan D9, D10).

The producer is customer-owned and runs offline, so the system, not a person, decides when an
automatic variant is due: the load reads the series' published cell-loss evidence before the
delivery's period, for the same contract, marketplace and period grain, and refuses a delivery of
any other variant with `PACK_VARIANT_MISMATCH` naming the one expected. Fine and coarse rows never
mix in one series, and a switch once made holds across a gap in publications (sticky, D10). When
that state can't be read, nothing is accepted: the expected variant reads `UNKNOWN_VARIANT`.

Pure: the evidence is read by an adapter and passed in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from domain.producer.models import VariantTrigger

if TYPE_CHECKING:  # pragma: no cover
    from domain.semantics.validation import PublicationEvidence

PACK_VARIANT_MISMATCH = "PACK_VARIANT_MISMATCH"
"""The typed load finding for a delivery of a variant other than the one expected."""

BASE_PACK = "the base pack"
"""How a finding names the expected binding when no variant is expected."""

UNKNOWN_VARIANT = "unknown"
"""How a finding names the expected binding when the series' switch state couldn't be read."""


@dataclass(frozen=True, slots=True)
class DeclaredVariant:
    """One declared variant as the load sees it: its id, pack version and trigger."""

    variant: str
    pack_version: int
    trigger: VariantTrigger


@dataclass(frozen=True, slots=True)
class PreviousPeriodEvidence:
    """The switch state a series' CURRENT publications before the delivery's period establish.

    `variant` is the automatic variant any earlier period published under (sticky), or None when
    every earlier period is the base pack's; `degraded` whether the latest earlier base-pack
    period's cell-loss evidence graded the delivery, or any slice of it, past the degrade
    threshold. `established` is False when the state couldn't be read (an unreadable period, a
    scope that can't be rebuilt, or a publication with no readable cell-loss record): the load
    then accepts no delivery of the pack rather than guessing the base pack.
    """

    variant: str | None
    degraded: bool
    established: bool = True


UNESTABLISHED = PreviousPeriodEvidence(variant=None, degraded=False, established=False)
"""The evidence a reader hands over when the series' switch state can't be read."""


@dataclass(frozen=True, slots=True)
class VariantRequest:
    """A delivery's declared variants, the variant it names, and the previous period's evidence.

    `requested` is the registration's `pack_variant` option; `delivered` the variant its governed
    pack version binds. They differ only on a malformed manifest, which is refused the same way.
    """

    declared: tuple[DeclaredVariant, ...]
    requested: str | None
    delivered: str | None
    previous: PreviousPeriodEvidence | None


@dataclass(frozen=True, slots=True)
class VariantSelection:
    """The variant expected for this period, and whether the delivery is that variant."""

    expected: str | None
    accepted: bool
    detail: str


NOT_DEGRADED_GRADE = "not_degraded"
"""The cell-loss grade (`LossGrade.NOT_DEGRADED`) that leaves an automatic variant off."""


def previous_period_evidence(
    evidence: PublicationEvidence, rule_id: str, variant: str | None
) -> PreviousPeriodEvidence:
    """Read one CURRENT publication's cell-loss record into what the variant check needs.

    `rule_id` is the contract's `suppression:<contract>.cell-loss.v1` evidence rule; `variant`
    the series the publication was found under (its lens names it). Degraded when the delivery's
    grade, or any graded slice's, is past not_degraded. A publication with no readable record is
    `UNESTABLISHED`: every discard-policy publication carries one, so its absence means the
    switch state can't be read, and the load fails closed rather than keep the base pack.
    """
    for result in evidence.results:
        if result.rule_id != rule_id:
            continue
        try:
            parsed = cast("object", json.loads(result.detail))
        except ValueError:
            break
        if not isinstance(parsed, dict) or "grade" not in parsed:
            break
        record = cast("dict[str, object]", parsed)
        raw_slices = record.get("slices")
        slices = cast("list[object]", raw_slices) if isinstance(raw_slices, list) else []
        grades = [record.get("grade")] + [
            cast("dict[str, object]", item).get("grade")
            for item in slices
            if isinstance(item, dict)
        ]
        return PreviousPeriodEvidence(
            variant=variant,
            degraded=any(grade != NOT_DEGRADED_GRADE for grade in grades),
        )
    return UNESTABLISHED


def _named(variant: str | None) -> str:
    return BASE_PACK if variant is None else f"variant {variant}"


def _due(request: VariantRequest) -> str | None:
    """Return the automatic variant due this period, or None.

    Sticky (plan D10, owner item S-B6): once any earlier period was delivered under an automatic
    variant, each later period is too, until Step B decides otherwise, whatever gap lies between.
    Otherwise a degraded latest earlier period makes the variant due from this one.
    """
    automatic = tuple(
        item.variant
        for item in request.declared
        if item.trigger is VariantTrigger.DEGRADE_NEXT_PERIOD
    )
    previous = request.previous
    if not automatic or previous is None:
        return None
    if previous.variant in automatic:
        return previous.variant
    return automatic[0] if previous.degraded else None


def select_pack_variant(request: VariantRequest) -> VariantSelection:
    """Decide the expected variant and whether the delivery matches it.

    - A pack declaring an automatic variant whose switch state couldn't be read accepts nothing.
    - An automatic variant that is due is the only accepted delivery.
    - Otherwise the base pack is expected, and a manual variant is accepted only when the
      registration names it.
    - An automatic variant that isn't due, an undeclared variant, or a registration whose named
      variant differs from its governed pack version is refused.
    """
    declared = {item.variant: item for item in request.declared}
    if request.requested != request.delivered:
        return VariantSelection(
            expected=request.requested,
            accepted=False,
            detail=(
                f"the registration names {_named(request.requested)} but its governed pack "
                f"version binds {_named(request.delivered)}."
            ),
        )
    delivered = request.delivered
    automatic = any(item.trigger is VariantTrigger.DEGRADE_NEXT_PERIOD for item in request.declared)
    if automatic and request.previous is not None and not request.previous.established:
        return VariantSelection(
            expected=UNKNOWN_VARIANT,
            accepted=False,
            detail=(
                "the series' switch state before this period couldn't be read, so no variant is "
                f"accepted until it can; this delivery is {_named(delivered)}."
            ),
        )
    due = _due(request)
    if due is not None:
        return VariantSelection(
            expected=due,
            accepted=delivered == due,
            detail=(
                f"{_named(due)} is due from the previous period's cell loss, so this delivery "
                f"must be it; it is {_named(delivered)}."
            ),
        )
    if delivered is None:
        return VariantSelection(None, True, f"{BASE_PACK} is expected and was delivered.")
    found = declared.get(delivered)
    if found is not None and found.trigger is VariantTrigger.MANUAL:
        return VariantSelection(
            delivered, True, f"manual {_named(delivered)} was named by the registration."
        )
    return VariantSelection(
        expected=None,
        accepted=False,
        detail=(
            f"{BASE_PACK} is expected: {_named(delivered)} isn't due, because no previous "
            "period's cell loss switched it on."
            if found is not None
            else f"{BASE_PACK} is expected: {_named(delivered)} isn't declared by this pack."
        ),
    )
