# SPDX-License-Identifier: MIT-0
"""Isolated bake refusals: the closed table of what a bake limit or failure is (ADR-142).

Every bake limit and failure is one of two classes. An **isolated refusal** is a scale limit, a
data-history gap or a dependency that stayed down: that item's, section's or page's own designed
absence, published with the rest of the page and alarmed once. A **defect** is anything else and
holds the cohort (dev's graph entry) or the page (the legacy entry). This module is the one home
of which reason is which class, which context each reason may carry, and which grain may claim
which scope.

`IsolatedRefusal` is a `DomainError` (code `BAKE_REFUSAL/1`) with no free text and no
identifiers: a closed reason, a closed scope, a count of refused items and only its reason's
allowlisted integer or `ADLY-*` code fields. Anything else raises `ValueError` at construction,
so a mis-keyed refusal is a defect a static scan finds before it can reach a task.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from domain.shared_kernel.errors import DomainError, ErrorCode, InvariantError, ValidationError

__all__ = [
    "BAKE_REFUSAL",
    "ISOLATABLE_SECTIONS",
    "ITEM_SECTIONS",
    "MEMORY_SIZED",
    "OPERATOR_ACTIONS",
    "PARTITION_BOUND_CODES",
    "REFUSAL_CLASSES",
    "REFUSAL_CONTEXT",
    "REFUSAL_SCOPE_INVALID",
    "STORED_REFUSAL_INVALID",
    "IsolatedRefusal",
    "IsolatedRefusalReason",
    "RefusalClass",
    "RefusalGroup",
    "RefusalScope",
    "check_refusal_scope",
    "classify_refusal",
    "group_refusals",
]

BAKE_REFUSAL: Final = ErrorCode("BAKE_REFUSAL", 1)
"""An isolated bake refusal: one item's, section's or page's designed absence."""
STORED_REFUSAL_INVALID: Final = ErrorCode("BAKE_REFUSAL", 2)
"""A settled task's stored refusal groups don't validate: a defect, never replayed."""
REFUSAL_SCOPE_INVALID: Final = ErrorCode("BAKE_REFUSAL", 3)
"""A source claimed a scope its page's declared sections don't allow: a defect."""

PARTITION_BOUND_CODES: Final = frozenset(
    {ErrorCode("PUBLICATION_SELECTION", 2), ErrorCode("PUBLICATION_SELECTION", 3)}
)
"""`SELECTION_TOO_LARGE` and `PACK_SNAPSHOT_TOO_LARGE`, the selector's exact-partition bounds.

Declared by `adapters.outbound.publication_selection`; a unit test holds the two equal."""

_MAX_FIELD: Final = 2**31
_ERROR_CODE: Final = re.compile(r"ADLY-[A-Z][A-Z0-9_]*-\d{4}")


class IsolatedRefusalReason(StrEnum):
    """Why a part of a page is its designed absence (closed)."""

    ROWS_OVER_CEILING = "rows_over_ceiling"
    BODY_OVER_CAP = "body_over_cap"
    BODIES_OVER_INVOCATION = "bodies_over_invocation"
    PARTITIONS_OVER_BOUND = "partitions_over_bound"
    STATEMENT_TIME_OVER_INVOCATION = "statement_time_over_invocation"
    POPULATION_OVER_CEILING = "population_over_ceiling"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    SOURCE_REFUSED = "source_refused"
    HISTORY_GAP = "history_gap"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"


class RefusalScope(StrEnum):
    """The grain a refusal lands on: the smallest one the failed read alone feeds."""

    ITEM = "item"
    SECTION = "section"
    PAGE = "page"


class RefusalClass(StrEnum):
    """What an operator does about a refusal: `limit` and `dependency` alarm, `data` counts."""

    LIMIT = "limit"
    DEPENDENCY = "dependency"
    DATA = "data"


_R = IsolatedRefusalReason
REFUSAL_CLASSES: Final[Mapping[IsolatedRefusalReason, RefusalClass]] = {
    _R.ROWS_OVER_CEILING: RefusalClass.LIMIT,
    _R.BODY_OVER_CAP: RefusalClass.LIMIT,
    _R.BODIES_OVER_INVOCATION: RefusalClass.LIMIT,
    _R.PARTITIONS_OVER_BOUND: RefusalClass.LIMIT,
    _R.STATEMENT_TIME_OVER_INVOCATION: RefusalClass.LIMIT,
    _R.POPULATION_OVER_CEILING: RefusalClass.LIMIT,
    _R.DEPENDENCY_UNAVAILABLE: RefusalClass.DEPENDENCY,
    _R.SOURCE_REFUSED: RefusalClass.DATA,
    _R.HISTORY_GAP: RefusalClass.DATA,
    _R.EVIDENCE_INCOMPLETE: RefusalClass.DATA,
}
"""The one class table: limit and dependency alarm; data is counted, not alarmed."""

_RAISE_MEMORY: Final = (
    "raise the Baker memory: set bake_tasks.baker_memory_mib, run make "
    "generate-execution-graph-protocol and make deploy-all; the ceiling grows with it "
    "(make check-bakeability prints it per size)"
)
OPERATOR_ACTIONS: Final[Mapping[IsolatedRefusalReason, str]] = {
    _R.ROWS_OVER_CEILING: _RAISE_MEMORY,
    _R.POPULATION_OVER_CEILING: _RAISE_MEMORY,
    _R.BODY_OVER_CAP: "report the page and variant: a body over the cap needs a source change",
    _R.BODIES_OVER_INVOCATION: "raise the Baker memory: it scales CPU and network for the writes",
    _R.PARTITIONS_OVER_BOUND: (
        "shrink the declared preset: the line states its partitions and the exact-read bound"
    ),
    _R.STATEMENT_TIME_OVER_INVOCATION: (
        "check the bake workgroup's Athena queueing; the page re-bakes on the next landing"
    ),
}
"""What an operator does about each `limit` refusal; the `isolated_refusal` line states it.

A unit test holds its keys equal to the `limit` reasons (ADR-152)."""
MEMORY_SIZED: Final = frozenset({_R.ROWS_OVER_CEILING, _R.POPULATION_OVER_CEILING})
"""The limits the Baker's memory sizes: their line also states the memory the model assumed."""

REFUSAL_CONTEXT: Final[Mapping[IsolatedRefusalReason, frozenset[str]]] = {
    _R.ROWS_OVER_CEILING: frozenset({"rows_read", "max_rows"}),
    # `encoded_bytes`, not `body_bytes`: the telemetry seams drop any key naming a body.
    _R.BODY_OVER_CAP: frozenset({"encoded_bytes", "max_bytes"}),
    _R.BODIES_OVER_INVOCATION: frozenset({"bodies", "max_bodies", "written"}),
    _R.PARTITIONS_OVER_BOUND: frozenset({"required_partitions", "maximum_partitions"}),
    _R.STATEMENT_TIME_OVER_INVOCATION: frozenset({"waited_seconds", "wait_limit_seconds"}),
    _R.POPULATION_OVER_CEILING: frozenset({"population", "guard"}),
    _R.DEPENDENCY_UNAVAILABLE: frozenset({"attempts", "error_code"}),
    _R.SOURCE_REFUSED: frozenset({"error_code"}),
    _R.HISTORY_GAP: frozenset(),
    _R.EVIDENCE_INCOMPLETE: frozenset(),
}
"""Each reason's closed context: integers in 0 <= v < 2**31, or an `ADLY-*` `error_code`."""

ISOLATABLE_SECTIONS: Final[Mapping[str, frozenset[str]]] = {
    "audiences": frozenset({"campaign-audiences"}),
    "new-customers": frozenset({"new-to-brand-step-2"}),
    "reach": frozenset({"frequency"}),
}
"""The sections that may be refused at section scope: those declaring `period_anchor: false`.

A section that doesn't declare the key is an anchor. A schema test holds this equal to
`presentation/pages/v2/pages.yaml`, so the set can't grow by omission."""

ITEM_SECTIONS: Final[Mapping[str, frozenset[str]]] = {
    "audiences": frozenset({"campaign-audiences", "segment-ranking"}),
}
"""The sections that hold items: an Audiences campaign, or an Audiences profile path (an item
of the anchor section `segment-ranking`). No other page has items."""


def _valid_context(reason: IsolatedRefusalReason, context: Mapping[str, object]) -> None:
    allowed = REFUSAL_CONTEXT[reason]
    for key, value in context.items():
        if key not in allowed:
            raise ValueError(f"{reason.value} does not carry context key {key!r}.")
        if key == "error_code":
            if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
                raise ValueError("an isolated refusal's error_code is an ADLY-* taxonomy code.")
        elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < _MAX_FIELD:
            raise ValueError(f"{reason.value}'s {key} is an integer in 0 <= value < 2**31.")


class IsolatedRefusal(DomainError):  # noqa: N818 -- ADR-142's name: a refusal, not an error
    """One item's, section's or page's designed absence (code `BAKE_REFUSAL/1`).

    `section` names the declared section an item or section refusal lands in; it is a
    presentation identifier, checked against the page's declared sections and never emitted.
    """

    def __init__(  # noqa: PLR0913 -- the closed reason, scope, count and section
        self,
        reason: IsolatedRefusalReason,
        scope: RefusalScope,
        count: int = 1,
        *,
        section: str | None = None,
        **context: int | str,
    ) -> None:
        if not isinstance(reason, IsolatedRefusalReason):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("an isolated refusal's reason is a closed IsolatedRefusalReason.")
        if not isinstance(scope, RefusalScope):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("an isolated refusal's scope is a closed RefusalScope.")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count < _MAX_FIELD:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("an isolated refusal refuses at least one item.")
        if section is not None and scope is RefusalScope.PAGE:
            raise ValueError("a page refusal names no section.")
        _valid_context(reason, context)
        super().__init__(
            BAKE_REFUSAL,
            "Part of this page is unavailable.",
            context={"reason": reason.value, "scope": scope.value, "count": count, **context},
        )
        self.reason = reason
        self.scope = scope
        self.count = count
        self.section = section
        self.refusal_context: Mapping[str, int | str] = dict(sorted(context.items()))

    @property
    def refusal_class(self) -> RefusalClass:
        """The reason's class (the one table)."""
        return REFUSAL_CLASSES[self.reason]


def classify_refusal(error: DomainError | ValidationError) -> IsolatedRefusal:
    """Map a source's refusal to its page-scope isolated refusal (an `IsolatedRefusal` passes).

    `PUBLICATION_SELECTION/2` and `/3` are `partitions_over_bound`, carrying the figures that
    decided them; any other refusal is `source_refused` with its taxonomy code.
    """
    if isinstance(error, IsolatedRefusal):
        return error
    if error.code in PARTITION_BOUND_CODES:
        figures = {
            key: value
            for key in ("required_partitions", "maximum_partitions")
            if isinstance(value := error.context.get(key), int)
            and not isinstance(value, bool)
            and 0 <= value < _MAX_FIELD
        }
        return IsolatedRefusal(
            _R.PARTITIONS_OVER_BOUND, RefusalScope.PAGE, **cast("dict[str, Any]", figures)
        )
    return IsolatedRefusal(_R.SOURCE_REFUSED, RefusalScope.PAGE, error_code=str(error.code))


def check_refusal_scope(refusal: IsolatedRefusal, page: str) -> IsolatedRefusal:
    """Refuse a scope claim the page's declared sections don't allow; return the refusal.

    A section claim must name a non-anchor section of the page; an item claim must name a
    section of a page that holds items. Either other claim, or one naming no section, is a
    source defect (`InvariantError`, `REFUSAL_SCOPE_INVALID`).
    """
    allowed = {
        RefusalScope.SECTION: ISOLATABLE_SECTIONS,
        RefusalScope.ITEM: ITEM_SECTIONS,
    }.get(refusal.scope)
    if allowed is not None and refusal.section not in allowed.get(page, frozenset()):
        raise InvariantError(
            REFUSAL_SCOPE_INVALID,
            "A bake source claimed a refusal scope its page does not declare.",
            context={"page": page, "scope": refusal.scope.value, "reason": refusal.reason.value},
        )
    return refusal


@dataclass(frozen=True, slots=True)
class RefusalGroup:
    """One `(scope, reason)` group of a receive's refusals: what is emitted and stored."""

    scope: RefusalScope
    reason: IsolatedRefusalReason
    count: int
    context: tuple[tuple[str, int | str], ...]

    @property
    def refusal_class(self) -> RefusalClass:
        """The reason's class (the one table)."""
        return REFUSAL_CLASSES[self.reason]

    def to_record(self) -> dict[str, object]:
        """Return the stored form: closed fields and the allowlisted context, no identifier."""
        return {
            "scope": self.scope.value,
            "reason": self.reason.value,
            "refusal_class": self.refusal_class.value,
            "count": self.count,
            "context": dict(self.context),
        }

    @classmethod
    def from_record(cls, record: object) -> RefusalGroup:
        """Rebuild a stored group through the refusal's own validation; refuse anything else."""
        if not isinstance(record, dict):
            raise ValueError("a stored refusal group is an object.")
        fields = cast("dict[str, object]", record)
        if set(fields) != {"scope", "reason", "refusal_class", "count", "context"}:
            raise ValueError("a stored refusal group has exactly its five fields.")
        context = fields["context"]
        if not isinstance(context, dict):
            raise ValueError("a stored refusal group's context is an object.")
        scope = RefusalScope(str(fields["scope"]))
        reason = IsolatedRefusalReason(str(fields["reason"]))
        count = fields["count"]
        try:
            refusal = IsolatedRefusal(
                reason, scope, cast("int", count), **cast("dict[str, Any]", context)
            )
        except TypeError as error:  # a stored key colliding with a parameter name
            raise ValueError("a stored refusal group's context is not allowlisted.") from error
        if fields["refusal_class"] != refusal.refusal_class.value:
            raise ValueError("a stored refusal group's class disagrees with its reason.")
        return cls(scope, reason, refusal.count, tuple(refusal.refusal_context.items()))


def _merged(
    first: Mapping[str, int | str], second: Mapping[str, int | str]
) -> dict[str, int | str]:
    """Merge two refusals' context: the larger figure for an integer, the first code."""
    merged = dict(first)
    for key, value in second.items():
        held = merged.get(key)
        if held is None:
            merged[key] = value
        elif isinstance(held, int) and isinstance(value, int):
            merged[key] = max(held, value)
    return merged


def group_refusals(refusals: Iterable[IsolatedRefusal]) -> tuple[RefusalGroup, ...]:
    """Group refusals by `(scope, reason)`, summing counts; sorted by `(scope, reason)`.

    Three campaigns and one profile path with `history_gap` are one group with `count=4`; an
    item `history_gap` beside a section `rows_over_ceiling` is two.
    """
    counts: dict[tuple[RefusalScope, IsolatedRefusalReason], int] = {}
    contexts: dict[tuple[RefusalScope, IsolatedRefusalReason], dict[str, int | str]] = {}
    for refusal in refusals:
        key = (refusal.scope, refusal.reason)
        counts[key] = counts.get(key, 0) + refusal.count
        contexts[key] = _merged(contexts.get(key, {}), refusal.refusal_context)
    return tuple(
        RefusalGroup(
            scope,
            reason,
            min(counts[(scope, reason)], _MAX_FIELD - 1),
            tuple(sorted(contexts[(scope, reason)].items())),
        )
        for scope, reason in sorted(counts, key=lambda key: (key[0].value, key[1].value))
    )
