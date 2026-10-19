# SPDX-License-Identifier: MIT-0
"""The D7 seam records the owner's answer (S6 §2.8, §2.9; decision log 2026-10-09).

Q1 is "refresh pending" and Q2 is body K ("keep the last version and record the reason"). Each
policy enum holds only its decided value, so neither call site has an unchosen body to reach.
The call sites' behaviour is asserted in `test_presentation_handler.py` and `test_graph_bake.py`.
"""

from __future__ import annotations

import pytest

from domain.presentation.d7_seam import (
    D7_ANSWER,
    D7Answer,
    OpenCohortPolicy,
    SourceUnavailablePolicy,
    open_cohort_policy,
    source_unavailable_policy,
)

pytestmark = [pytest.mark.unit]


def test_the_build_carries_the_owners_d7_answer() -> None:
    decided = D7Answer(
        open_cohort=OpenCohortPolicy.REFRESH_PENDING,
        source_unavailable=SourceUnavailablePolicy.KEEP_LAST_BOUND,
    )
    assert decided == D7_ANSWER
    assert open_cohort_policy() is OpenCohortPolicy.REFRESH_PENDING
    assert source_unavailable_policy() is SourceUnavailablePolicy.KEEP_LAST_BOUND


def test_only_the_chosen_bodies_exist() -> None:
    """PREDECESSOR (case 4's fallback) and NOT_AVAILABLE (body N, D27(b)) are not built."""
    assert [policy.value for policy in OpenCohortPolicy] == ["refresh_pending"]
    assert [policy.value for policy in SourceUnavailablePolicy] == ["keep_last_bound"]
