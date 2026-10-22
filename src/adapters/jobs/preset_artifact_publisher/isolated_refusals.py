# SPDX-License-Identifier: MIT-0
"""The context-local collector of a page's item and section refusals (ADR-142).

A source records an item or section refusal with `note_isolated_refusal` and keeps rendering:
the absence is part of the body, which publishes. Page scope is never noted; the source raises
`IsolatedRefusal(scope=page)` instead. Only `render_page` opens the collector, which both bake
entries call, so a note made anywhere else (the graph entry's plan pass, which prefetches and
binds before the pinned render) is discarded by design: that render is replayed and noted then.

The same pattern as `collecting()` for Athena statistics: one list per open scope, shared by the
statement threads, which copy the caller's context.

`capped_preset_artifact_json` is every source's body-size check before staging (02 §3.4): a body
over `MAX_PRESET_ARTIFACT_BYTES` refuses the page (`body_over_cap`) before any object is built,
so the D18 ceiling stays a last-line invariant a source never reaches.
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
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    canonical_preset_artifact_json,
)

__all__ = ["capped_preset_artifact_json", "collecting_refusals", "note_isolated_refusal"]

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


def capped_preset_artifact_json(value: dict[str, object]) -> str:
    """Render a body's canonical JSON, refusing the page when it exceeds the artifact cap.

    The encoded size is checked before the body (or custody) object is constructed, so an
    over-cap body is the page's `body_over_cap` absence, never the D18 defect.
    """
    payload = canonical_preset_artifact_json(value)
    size = len(payload.encode())
    if size > MAX_PRESET_ARTIFACT_BYTES:
        raise IsolatedRefusal(
            IsolatedRefusalReason.BODY_OVER_CAP,
            RefusalScope.PAGE,
            encoded_bytes=size,
            max_bytes=MAX_PRESET_ARTIFACT_BYTES,
        )
    return payload
