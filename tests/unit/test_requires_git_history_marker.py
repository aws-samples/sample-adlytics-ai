# SPDX-License-Identifier: MIT-0
"""The requires_git_history marker skips with a named reason outside the revision's history.

Each case builds its own repository under `tmp_path`, so the control holds in a clean export,
which has no history of its own: the absent revision skips with the reason, and a present commit
runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gate_script_support import commit, new_repo
from git_history_support import (
    has_commit,
    marker_revision,
    missing_history_reason,
    skip_reason,
)

pytestmark = pytest.mark.unit

# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
ABSENT = "0123456789abcdef0123456789abcdef01234567"


def test_an_absent_revision_skips_with_a_reason_naming_it(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)

    assert missing_history_reason([ABSENT], repo) == (
        f"requires git history: {ABSENT} is not in this checkout (for example a clean export)"
    )


def test_a_present_commit_runs(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    head = commit(repo, "b.txt", "second\n")

    assert has_commit(head, repo)
    assert has_commit("HEAD", repo)
    assert missing_history_reason([head, "HEAD"], repo) is None


def test_a_directory_without_a_repository_skips(tmp_path: Path) -> None:
    exported = tmp_path / "export"
    exported.mkdir()

    assert missing_history_reason(["HEAD"], exported) == skip_reason("HEAD")


def test_the_first_absent_revision_is_the_one_named(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)

    assert missing_history_reason(["HEAD", ABSENT], repo) == skip_reason(ABSENT)


def test_a_marker_needs_exactly_one_revision() -> None:
    assert marker_revision(pytest.mark.requires_git_history("HEAD").mark) == "HEAD"
    for bad in (
        pytest.mark.requires_git_history().mark,
        pytest.mark.requires_git_history("HEAD", "main").mark,
        pytest.mark.requires_git_history("").mark,
        pytest.mark.requires_git_history(revision="HEAD").mark,
    ):
        with pytest.raises(pytest.UsageError, match="exactly one non-empty revision"):
            marker_revision(bad)
