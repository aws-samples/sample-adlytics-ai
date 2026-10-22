# SPDX-License-Identifier: MIT-0
"""The context-local collector of a page's item and section refusals (ADR-142).

A source records an item or section refusal with `note_isolated_refusal` and keeps rendering:
the absence is part of the body, which publishes. Page scope is never noted; the source raises
`IsolatedRefusal(scope=page)` instead. Only `render_page` opens the collector, which both bake
entries call, so a note made anywhere else (the graph entry's plan pass, which prefetches and
binds before the pinned render) is discarded by design: that render is replayed and noted then.

The same pattern as `collecting()` for Athena statistics: one list per open scope, shared by the
statement threads, which copy the caller's context.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from domain.operations.bake_refusals import (
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)

__all__ = ["collecting_refusals", "note_isolated_refusal"]

_NOTES: ContextVar[list[IsolatedRefusal] | None] = ContextVar(
    "bake_isolated_refusals", default=None
)


@contextmanager
def collecting_refusals() -> Generator[list[IsolatedRefusal]]:
    """Collect the item and section refusals noted inside one page's render."""
    notes: list[IsolatedRefusal] = []
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)


def note_isolated_refusal(  # noqa: PLR0913 -- the closed reason, scope, count and section
    reason: IsolatedRefusalReason,
    scope: RefusalScope,
    count: int = 1,
    *,
    section: str,
    **context: int | str,
) -> None:
    """Record `count` items (or one section) of `section` refused for `reason`.

    The refusal is validated at once (an unknown context key raises `ValueError`), whether or
    not a collector is open, so a mis-keyed note fails in every caller.
    """
    if scope is RefusalScope.PAGE:
        raise ValueError("a page refusal is raised, not noted.")
    refusal = IsolatedRefusal(reason, scope, count, section=section, **context)
    notes = _NOTES.get()
    if notes is not None:
        notes.append(refusal)
