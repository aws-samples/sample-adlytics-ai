# SPDX-License-Identifier: MIT-0
"""The D7 seam: the only code whose behaviour depends on the owner's answer to D7 (S6 §2.8, §2.9).

D7 asks two questions. The owner answered both on 2026-10-09 (decision log, "Owner decisions,
2026-10-09 about 16:05 UTC"), and this module records the answers as `D7_ANSWER`:

- **Q1, the open cohort** (§2.9 case 4): `REFRESH_PENDING`. While a grant's cohort is open, a
  page whose pointer carries that grant is not available (`serving_refresh_pending`) until bake
  binds. Case 4's predecessor fallback is not built, so presentation makes no `REVISION#` read.
- **Q2, a source-unavailable intent** (§2.8 step 4): `KEEP_LAST_BOUND` (body K). The bake
  writes nothing, the page keeps its last bound pointer, and the run records the intent
  `unavailable` with its reason. Body N's unavailable pointer and D27(b)'s restore write are
  not built.

Each policy enum holds only the decided value, so there is one body per question. The two call
sites, `serve_open_cohort` (presentation) and `apply_source_unavailable` (the graph bake), match
on the accessor's value. Adding an alternative back is a reviewed change: pyright's exhaustive
`match` check refuses it until its body exists. Pure domain: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "D7_ANSWER",
    "D7Answer",
    "OpenCohortPolicy",
    "SourceUnavailablePolicy",
    "open_cohort_policy",
    "source_unavailable_policy",
]


class OpenCohortPolicy(StrEnum):
    """D7 Q1: what a page shows while its grant's cohort is open (§2.9 case 4)."""

    REFRESH_PENDING = "refresh_pending"
    """Not available (`serving_refresh_pending`) until bake binds; no `REVISION#` read."""


class SourceUnavailablePolicy(StrEnum):
    """D7 Q2: what a graph bake writes for a source-unavailable intent (§2.8 step 4)."""

    KEEP_LAST_BOUND = "keep_last_bound"
    """Body K: write nothing; the page keeps its last bound pointer; the run records the reason."""


@dataclass(frozen=True, slots=True)
class D7Answer:
    """The owner's answer to D7's two questions."""

    open_cohort: OpenCohortPolicy
    source_unavailable: SourceUnavailablePolicy


D7_ANSWER: Final = D7Answer(
    open_cohort=OpenCohortPolicy.REFRESH_PENDING,
    source_unavailable=SourceUnavailablePolicy.KEEP_LAST_BOUND,
)
"""The owner's D7 answer (decision log, 2026-10-09): "refresh pending", and body K."""


def open_cohort_policy(answer: D7Answer = D7_ANSWER) -> OpenCohortPolicy:
    """Return the Q1 policy presentation applies to an open cohort's pointer."""
    return answer.open_cohort


def source_unavailable_policy(answer: D7Answer = D7_ANSWER) -> SourceUnavailablePolicy:
    """Return the Q2 policy the graph bake applies to a source-unavailable intent."""
    return answer.source_unavailable
