# SPDX-License-Identifier: MIT-0
"""The model artifact digest.

If the operator's and the job's digests disagree on the same files, every run fails
MODEL_ARTIFACT_MISMATCH; if they agree on different files, a swapped model runs.
"""

from __future__ import annotations

import pytest

from domain.forecasting.artifact_roster import RosterEntry, roster_digest

pytestmark = pytest.mark.unit

A = RosterEntry("config.json", 10, "a" * 64)
B = RosterEntry("model.safetensors", 20, "b" * 64)


def test_the_digest_ignores_listing_order() -> None:
    assert roster_digest([A, B]) == roster_digest([B, A])


def test_the_digest_is_sha256_of_the_compact_sorted_json_roster() -> None:
    """Pinned: the operator command and the job must agree byte for byte on this form."""
    assert roster_digest([A, B]) == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "5a743d970dc599b7bc26e166be897812219eaaf4d53cc787f4c160798b410c2e"
    )


@pytest.mark.parametrize(
    "changed",
    [
        RosterEntry("model.safetensors", 21, "b" * 64),
        RosterEntry("model.safetensors", 20, "c" * 64),
        RosterEntry("weights.safetensors", 20, "b" * 64),
    ],
)
def test_a_changed_size_content_or_path_changes_the_digest(changed: RosterEntry) -> None:
    assert roster_digest([A, changed]) != roster_digest([A, B])


def test_a_repeated_path_an_escaping_path_and_an_empty_roster_are_refused() -> None:
    with pytest.raises(ValueError, match="appears twice"):
        roster_digest([A, A])
    with pytest.raises(ValueError, match="relative and inside"):
        RosterEntry("../config.json", 1, "a" * 64)
    with pytest.raises(ValueError, match="at least one file"):
        roster_digest([])
