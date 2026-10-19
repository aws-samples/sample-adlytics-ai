# SPDX-License-Identifier: MIT-0
"""Which preset pointers presentation serves, and the generation label it prints (S6 §2.9).

Each preset pointer from a roster read falls in one case (§2.9 "Presentation"; D28):

1. **No `grant_id`:** legacy history, served. The legacy-pointer mode `ConditionCheck` means no
   grant-less pointer is written after the `GRAPH` CAS, so no read-side check is made.
3. **`grant_id` G, `SERVING#G` present:** served only when the binding names the pointer's
   `serving_generation` (`require_granted_generation`); otherwise not available
   (`ungranted_generation`).
4. **`grant_id` G, `SERVING#G` absent** (the cohort is still open): the D7 seam's Q1 answer,
   "refresh pending", applies through `serve_open_cohort`. The page is not available
   (`serving_refresh_pending`) until bake binds, and no `REVISION#` read is made. Case 2 (an
   unavailable pointer) existed only under D7 body N, which the owner didn't choose.

The served pointer's generation label (D27(a) condition, D27 review M1) is one closed value:
`built_at` (the grant-bearing pointer's `written_at`) or `built_before` (the `GRAPH` entry
instant, for a grant-less pointer in `GRAPH`), and none otherwise. Pure domain: no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn

from domain.operations.execution_graph_release import (
    ServingBinding,
    require_granted_generation,
)
from domain.operations.stuck_flow import FINALIZER_BLOCKED_AFTER
from domain.presentation.d7_seam import OpenCohortPolicy, open_cohort_policy
from domain.presentation.preset_artifacts import PresetArtifactPointer
from domain.shared_kernel.errors import (
    GRAPH_SERVING_ENTRY_MALFORMED,
    InvariantError,
)

__all__ = [
    "SERVING_REFRESH_PENDING_REASON",
    "SERVING_REFUSAL_SETTLE",
    "UNGRANTED_GENERATION_REASON",
    "PageGeneration",
    "RefreshPendingError",
    "granted_generation",
    "legacy_generation",
    "normalize_graph_entered_at",
    "refusal_age_seconds",
    "serve_open_cohort",
]

UNGRANTED_GENERATION_REASON: Final = "ungranted_generation"
"""The not-available reason for a case-3 pointer whose binding doesn't name its generation."""
SERVING_REFRESH_PENDING_REASON: Final = "serving_refresh_pending"
"""The not-available reason for a case-4 pointer whose cohort is still open (D7 Q1)."""
SERVING_REFUSAL_SETTLE: Final = FINALIZER_BLOCKED_AFTER
"""ADR-139: a refusal older than this after its bind is not a settling cohort (270 s, DB26).

`CURRENT` and its binding commit in one transaction and pointers move only under an authorizing
binding, so a V3 writer cannot produce a refusal at all; a `within_settle` one is a defect signal
too, recorded but not paged."""


class RefreshPendingError(Exception):
    """Case 4 under D7 Q1 "refresh pending": the pointer's grant isn't bound yet, so not served."""

    def __init__(self) -> None:
        super().__init__(SERVING_REFRESH_PENDING_REASON)
        self.reason = SERVING_REFRESH_PENDING_REASON


_INSTANT: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_INSTANT_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def normalize_graph_entered_at(value: object) -> str:
    """Render the mode item's `graph_entered_at` as a whole-second UTC instant, rounded up.

    B4 (D27R2-1 option (a)): the value is the `GRAPH` CAS receipt time, so as a `built_before`
    bound it is accurate to within the commit latency and the host clock. Rounding a fractional
    second up keeps the bound an upper bound. A value that isn't a timezone-aware ISO 8601
    instant is an invariant failure on the mode read.
    """
    if not isinstance(value, str):
        raise _entry_malformed()
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise _entry_malformed() from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise _entry_malformed()
    instant = instant.astimezone(UTC)
    if instant.microsecond:
        instant = instant.replace(microsecond=0) + timedelta(seconds=1)
    return instant.strftime(_INSTANT_FORMAT)


def _entry_malformed() -> InvariantError:
    return InvariantError(
        GRAPH_SERVING_ENTRY_MALFORMED,
        "the GRAPH mode item's entry instant is not a whole-second UTC instant.",
    )


@dataclass(frozen=True, slots=True)
class PageGeneration:
    """The served pointer's build time: exactly one of `built_at` or `built_before`."""

    built_at: str | None = None
    built_before: str | None = None

    def __post_init__(self) -> None:
        values = [value for value in (self.built_at, self.built_before) if value is not None]
        if len(values) != 1:
            raise ValueError("a page generation carries exactly one of built_at or built_before.")
        if not _INSTANT.fullmatch(values[0]):
            raise ValueError("a page generation instant is not RFC 3339 whole seconds.")


def legacy_generation(graph_entered_at: str | None) -> PageGeneration | None:
    """Case 1: a grant-less pointer is served; in `GRAPH` it was built before the CAS."""
    return None if graph_entered_at is None else PageGeneration(built_before=graph_entered_at)


def granted_generation(
    pointer: PresetArtifactPointer, binding: ServingBinding | None
) -> PageGeneration:
    """Cases 3 and 4: serve a grant-bearing pointer only under a binding that names it.

    Raises `UngrantedGenerationError` when the present binding doesn't name the pointer's
    `serving_generation`, and `RefreshPendingError` when the binding is absent (case 4).
    """
    if pointer.grant_id is None or pointer.serving_generation is None:
        raise ValueError("a granted generation needs a grant-bearing pointer.")
    if binding is None:
        return serve_open_cohort()
    require_granted_generation(
        {binding.grant_id: binding}, pointer.serving_generation, graph_authority=True
    )
    return PageGeneration(built_at=pointer.written_at)


def serve_open_cohort() -> NoReturn:
    """Case 4: apply the D7 seam's Q1 answer to a pointer whose cohort is still open.

    "Refresh pending": the pointer isn't served until bake binds its grant, so this raises
    `RefreshPendingError` (not available, `serving_refresh_pending`). No `REVISION#` read is
    made, because the predecessor fallback isn't built.
    """
    match open_cohort_policy():
        case OpenCohortPolicy.REFRESH_PENDING:
            raise RefreshPendingError


def refusal_age_seconds(
    pointer: PresetArtifactPointer, binding: ServingBinding | None, now: datetime
) -> int:
    """How long a refused pointer has been refused: since its bind, else since it was written.

    Case 3 anchors on the binding's `bound_at` ("after the last bind"); case 4 (no binding) on the
    pointer's own `written_at`. A missing anchor, a `written_at` that is not a timezone-aware
    ISO 8601 instant, or a negative age is 0, so the age never turns the 409 refusal into a 500.
    """
    anchor = binding.bound_at if binding is not None else _written_at(pointer.written_at)
    if anchor is None:
        return 0
    return max(0, int((now - anchor).total_seconds()))


def _written_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if instant.utcoffset() is None else instant
