# SPDX-License-Identifier: MIT-0
"""scripts/review-entry.sh refuses a review not bound to a clean tree at the pinned commit.

Reviews that ran on dirty trees, or against pins behind the branch tip, judged code that was not
the code merged. Each refusal is paired with the passing entry it departs from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gate_script_support import commit, git, new_repo, script

pytestmark = pytest.mark.unit


def _entry(repo: Path, *args: str) -> tuple[int, str]:
    result = script("review-entry.sh", *args, cwd=repo)
    return result.returncode, result.stdout + result.stderr


def _passing_log(repo: Path, log: Path) -> Path:
    assert script("gate-log.sh", str(log), "true", cwd=repo).returncode == 0
    return log


def test_a_clean_tree_at_the_pin_enters(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")

    assert _entry(repo, "--pin", head) == (0, f"REVIEW_ENTRY_OK head={head}\n")


def test_a_dirty_tree_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "scratch.txt").write_text("untracked\n")

    code, out = _entry(repo, "--pin", head)

    assert code == 3
    assert out.startswith("REVIEW_ENTRY_REFUSED DIRTY_TREE porcelain_count=1")


def test_a_pin_behind_head_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    old = git(repo, "rev-parse", "HEAD")
    commit(repo, "b.txt", "second\n")

    code, out = _entry(repo, "--pin", old)

    assert code == 4
    assert out.startswith("REVIEW_ENTRY_REFUSED PIN_NOT_HEAD")


def test_an_unresolvable_pin_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)

    code, out = _entry(repo, "--pin", "no-such-revision")

    assert code == 4
    assert out.startswith("REVIEW_ENTRY_REFUSED PIN_UNRESOLVED no-such-revision")


def test_a_pin_behind_the_named_branch_tip_is_refused_and_its_tip_accepted(
    tmp_path: Path,
) -> None:
    repo = new_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "feature")
    git(repo, "checkout", "-q", "feature")
    commit(repo, "b.txt", "ahead\n")
    git(repo, "checkout", "-q", "main")

    behind_code, behind_out = _entry(repo, "--pin", head, "--branch", "feature")
    main_code, _ = _entry(repo, "--pin", head, "--branch", "main")
    missing_code, missing_out = _entry(repo, "--pin", head, "--branch", "absent")

    assert behind_code == 4
    assert behind_out.startswith("REVIEW_ENTRY_REFUSED PIN_NOT_BRANCH_TIP")
    assert main_code == 0
    assert missing_code == 4
    assert missing_out.startswith("REVIEW_ENTRY_REFUSED BRANCH_MISSING refs/heads/absent")


def test_missing_or_empty_evidence_is_refused_and_present_evidence_accepted(
    tmp_path: Path,
) -> None:
    repo = new_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    present = tmp_path / "evidence.md"
    present.write_text("decisive line\n")
    empty = tmp_path / "empty.md"
    empty.write_text("")
    empty_dir = tmp_path / "empty-dir"
    empty_dir.mkdir()

    assert _entry(repo, "--pin", head, "--evidence", str(present))[0] == 0
    missing = _entry(repo, "--pin", head, "--evidence", str(tmp_path / "absent.md"))
    blank = _entry(repo, "--pin", head, "--evidence", str(present), "--evidence", str(empty))
    hollow = _entry(repo, "--pin", head, "--evidence", str(empty_dir))

    assert missing[0] == 5
    assert missing[1].startswith("REVIEW_ENTRY_REFUSED EVIDENCE_MISSING")
    assert blank[0] == 5
    assert blank[1].startswith(f"REVIEW_ENTRY_REFUSED EVIDENCE_EMPTY {empty}")
    assert hollow[0] == 5
    assert hollow[1].startswith(f"REVIEW_ENTRY_REFUSED EVIDENCE_EMPTY {empty_dir}")


def test_a_gate_log_of_another_commit_is_refused_and_its_own_accepted(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    stale = _passing_log(repo, tmp_path / "stale.log")
    head = commit(repo, "b.txt", "second\n")
    fresh = _passing_log(repo, tmp_path / "fresh.log")

    assert _entry(repo, "--pin", head, "--gate-log", str(fresh))[0] == 0
    code, out = _entry(repo, "--pin", head, "--gate-log", str(fresh), "--gate-log", str(stale))

    assert code == 6
    assert out.startswith(f"REVIEW_ENTRY_REFUSED GATE_LOG_REJECTED {stale}:")
    assert "HEAD_MISMATCH" in out


def test_a_failed_gate_log_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    log = tmp_path / "failed.log"
    assert script("gate-log.sh", str(log), "false", cwd=repo).returncode == 1

    code, out = _entry(repo, "--pin", head, "--gate-log", str(log))

    assert code == 6
    assert "GATE_FAILED exit=1" in out


@pytest.mark.parametrize(
    "argv",
    [[], ["--branch", "main"], ["--pin"], ["--pin", "HEAD", "--unknown", "x"], ["--pin", ""]],
)
def test_a_usage_error_exits_2(tmp_path: Path, argv: list[str]) -> None:
    repo = new_repo(tmp_path)

    code, out = _entry(repo, *argv)

    assert code == 2
    assert "usage: review-entry.sh" in out
