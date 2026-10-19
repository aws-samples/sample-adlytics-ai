# SPDX-License-Identifier: MIT-0
"""Review-boundary controls for the ingestion traceability implementation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
IMPLEMENTATION_BASE = "82f1958a973295e2253b71755c8c5804fd264779"
CHECKPOINT_BASE = "dbd38b70"
REPOSITORY_ROOT = Path(__file__).parents[2]


@dataclass(frozen=True, slots=True)
class ReviewMetadata:
    """The two claims that must remain separate in implementation review metadata."""

    range_start: str
    inherited_branch_history_was_local_only: bool

    def validate(self) -> None:
        """Reject a range or locality claim that absorbs the inherited checkpoint."""
        resolved_start = subprocess.run(  # noqa: S603 - fixed git argv, no shell, no external input
            ["git", "rev-parse", self.range_start],  # noqa: S607
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if resolved_start != IMPLEMENTATION_BASE:
            raise ValueError("implementation review must begin at the traceability design base")
        if self.inherited_branch_history_was_local_only:
            raise ValueError("local-only applies only to successor checkpoint side effects")


def test_implementation_review_begins_at_the_approved_design_base() -> None:
    ReviewMetadata(IMPLEMENTATION_BASE, False).validate()


# Resolves a short revision, which needs the commit itself; the full-SHA tests above and below do
# not, because `git rev-parse` returns a full SHA without reading the object database.
@pytest.mark.requires_git_history(CHECKPOINT_BASE)
def test_review_metadata_rejects_the_inherited_checkpoint_base() -> None:
    with pytest.raises(ValueError, match="must begin"):
        ReviewMetadata(CHECKPOINT_BASE, False).validate()


def test_review_metadata_rejects_a_local_only_claim_about_inherited_history() -> None:
    with pytest.raises(ValueError, match="successor checkpoint side effects"):
        ReviewMetadata(IMPLEMENTATION_BASE, True).validate()
