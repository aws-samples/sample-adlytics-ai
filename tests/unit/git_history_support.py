# SPDX-License-Identifier: MIT-0
"""The `requires_git_history(revision)` marker: skip, naming the revision, outside its history.

A test that resolves a historical commit fails in a clean export (`git archive HEAD | tar -x`),
where the commit is not present, and that failure says nothing about the code under test. The
marker turns it into a skip whose reason names the revision, so the run reports what it could not
check instead of failing for an unrelated reason or passing silently. `tests/unit/conftest.py`
applies it in `pytest_runtest_setup`.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

MARKER = "requires_git_history"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def skip_reason(revision: str) -> str:
    """Return the skip reason for one revision absent from this checkout."""
    return f"requires git history: {revision} is not in this checkout (for example a clean export)"


def marker_revision(mark: pytest.Mark) -> str:
    """Return the marker's one revision, refusing any other argument shape."""
    if len(mark.args) != 1 or mark.kwargs or not isinstance(mark.args[0], str) or not mark.args[0]:
        raise pytest.UsageError(f"{MARKER} takes exactly one non-empty revision string")
    return mark.args[0]


def has_commit(revision: str, root: Path = REPOSITORY_ROOT) -> bool:
    """Return whether `revision` names a commit in `root`'s git history.

    False when git is not installed, `root` is not a repository, or the object is absent: each
    means the history the test needs is not here.
    """
    git = shutil.which("git")
    if git is None:
        return False
    # A-SUBPROCESS-001: fixed git argv; see docs/security-dispositions.md
    # The revision is a test constant passed through the marker; no caller input reaches it.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    result = subprocess.run(  # noqa: S603
        [git, "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def missing_history_reason(revisions: Iterable[str], root: Path = REPOSITORY_ROOT) -> str | None:
    """Return the skip reason for the first revision absent from `root`, else None."""
    for revision in revisions:
        if not has_commit(revision, root):
            return skip_reason(revision)
    return None
