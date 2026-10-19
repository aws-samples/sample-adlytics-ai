# SPDX-License-Identifier: MIT-0
"""scripts/gate-log.sh binds a gate run to its HEAD, tree and porcelain, and verifies the binding.

A gate log is cited as evidence for one commit. Each refusal below is paired with the clean run it
departs from, so a verify that accepted everything, or refused everything, fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gate_script_support import commit, git, new_repo, script

pytestmark = pytest.mark.unit


def _gate(repo: Path, log: Path, *command: str) -> int:
    return script("gate-log.sh", str(log), *command, cwd=repo).returncode


def _verify(repo: Path, log: Path, *extra: str) -> tuple[int, str]:
    result = script("gate-log.sh", "--verify", str(log), *extra, cwd=repo)
    return result.returncode, result.stdout + result.stderr


def test_a_passing_gate_on_a_clean_tree_verifies(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "pass.log"

    assert _gate(repo, log, "true") == 0
    code, out = _verify(repo, log)

    head = git(repo, "rev-parse", "HEAD")
    lines = log.read_text().splitlines()
    assert lines[0] == "GATE-LOG v2"
    assert f"head={head}" in lines
    assert f"tree={git(repo, 'rev-parse', 'HEAD^{tree}')}" in lines
    assert "branch=main" in lines
    assert "porcelain_count=0" in lines
    assert lines[-1].startswith("exit=0 utc_end=")
    assert f" post_head={head} post_tree={git(repo, 'rev-parse', 'HEAD^{tree}')} " in lines[-1]
    assert " post_porcelain_count=0 nonce=" in lines[-1]
    assert (code, out.strip()) == (0, f"GATE_LOG_OK head={head} log={log}")


def test_an_existing_log_file_is_refused_and_left_unchanged(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "taken.log"
    log.write_text("another run's evidence\n")

    result = script("gate-log.sh", str(log), "true", cwd=repo)

    assert result.returncode == 2
    assert "GATE_LOG_REFUSED LOG_EXISTS" in result.stderr
    assert log.read_text() == "another run's evidence\n"


def test_a_failing_gate_records_its_exit_and_verify_refuses_it(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "fail.log"

    assert _gate(repo, log, "false") == 1
    code, out = _verify(repo, log)

    assert log.read_text().splitlines()[-1].startswith("exit=1 utc_end=")
    assert code == 5
    assert "GATE_LOG_REFUSED GATE_FAILED exit=1" in out


def test_command_output_is_captured_between_header_and_footer(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "out.log"

    assert _gate(repo, log, "/bin/sh", "-c", "echo to-stdout; echo to-stderr >&2") == 0

    text = log.read_text()
    assert "to-stdout\n" in text
    assert "to-stderr\n" in text
    assert _verify(repo, log)[0] == 0


def test_a_log_taken_on_a_dirty_tree_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    (repo / "untracked.txt").write_text("not committed\n")
    log = tmp_path / "dirty.log"

    assert _gate(repo, log, "true") == 0
    (repo / "untracked.txt").unlink()
    code, out = _verify(repo, log)

    assert "porcelain_line=?? untracked.txt" in log.read_text().splitlines()
    assert code == 4
    assert "GATE_LOG_REFUSED DIRTY_TREE porcelain_count=1" in out


def test_a_passing_gate_that_edits_tracked_source_is_refused_and_a_read_only_one_verifies(
    tmp_path: Path,
) -> None:
    repo = new_repo(tmp_path)
    reads = tmp_path / "reads.log"
    edits = tmp_path / "edits.log"

    assert _gate(repo, reads, "/bin/sh", "-c", "cat a.txt") == 0
    assert _gate(repo, edits, "/bin/sh", "-c", "echo rewritten >> a.txt") == 0
    reads_code, reads_out = _verify(repo, reads)
    edits_code, edits_out = _verify(repo, edits)

    head = git(repo, "rev-parse", "HEAD")
    assert (reads_code, reads_out.strip()) == (0, f"GATE_LOG_OK head={head} log={reads}")
    assert " post_porcelain_count=1 nonce=" in edits.read_text().splitlines()[-1]
    assert (edits_code, edits_out.strip()) == (
        4,
        "GATE_LOG_REFUSED DIRTY_TREE post_porcelain_count=1 after the gate ran",
    )


def test_a_gate_that_moves_head_is_refused_for_the_commit_it_started_on(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    first = git(repo, "rev-parse", "HEAD")
    log = tmp_path / "commits.log"
    commits = (
        "git -c user.name=fixture -c user.email=fixture@example.invalid -c commit.gpgsign=false "
        "commit -q --allow-empty -m during-the-gate"
    )

    assert _gate(repo, log, "/bin/sh", "-c", commits) == 0
    second = git(repo, "rev-parse", "HEAD")
    code, out = _verify(repo, log, "--pin", first)

    tree = git(repo, "rev-parse", "HEAD^{tree}")
    assert second != first
    assert (code, out.strip()) == (
        7,
        f"GATE_LOG_REFUSED STATE_CHANGED head={first} post_head={second} "
        f"tree={tree} post_tree={tree}",
    )


def test_a_log_inside_the_checkout_excludes_only_itself(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    (repo / "logs").mkdir()
    alone = repo / "logs" / "alone.log"
    beside = repo / "logs" / "beside.log"

    assert _gate(repo, alone, "true") == 0
    alone_code, alone_out = _verify(repo, alone)
    alone.unlink()
    assert _gate(repo, beside, "/bin/sh", "-c", "echo stray > logs/stray.txt") == 0
    beside_code, beside_out = _verify(repo, beside)

    head = git(repo, "rev-parse", "HEAD")
    assert (alone_code, alone_out.strip()) == (0, f"GATE_LOG_OK head={head} log={alone}")
    assert (beside_code, beside_out.strip()) == (
        4,
        "GATE_LOG_REFUSED DIRTY_TREE post_porcelain_count=1 after the gate ran",
    )


def test_a_log_without_post_state_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "old-footer.log"
    assert _gate(repo, log, "true") == 0
    lines = log.read_text().splitlines()
    exit_part, _, rest = lines[-1].partition(" post_head=")
    old_footer = f"{exit_part} nonce={rest.rpartition(' nonce=')[2]}"
    log.write_text("\n".join([*lines[:-1], old_footer]) + "\n")

    code, out = _verify(repo, log)

    assert (code, out.strip()) == (
        6,
        "GATE_LOG_REFUSED LOG_TRUNCATED the last line is not this run's exit footer",
    )


def test_a_log_of_another_commit_is_refused_and_its_own_pin_accepted(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    first = git(repo, "rev-parse", "HEAD")
    log = tmp_path / "old.log"
    assert _gate(repo, log, "true") == 0
    commit(repo, "b.txt", "second\n")

    stale_code, stale_out = _verify(repo, log)
    pinned_code, _ = _verify(repo, log, "--pin", first)
    short_code, _ = _verify(repo, log, "--pin", first[:10])

    assert stale_code == 3
    assert "GATE_LOG_REFUSED HEAD_MISMATCH" in stale_out
    assert (pinned_code, short_code) == (0, 0)


def test_a_log_cut_off_before_its_footer_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "cut.log"
    assert _gate(repo, log, "true") == 0
    lines = log.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:-1]))

    code, out = _verify(repo, log)

    assert code == 6
    assert "GATE_LOG_REFUSED LOG_TRUNCATED" in out


def test_output_imitating_a_footer_does_not_finish_a_cut_off_log(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "spoof.log"
    zeros = "0" * 40
    imitation = (
        f"echo exit=0 utc_end=2026-01-01T00:00:00Z post_head={zeros} post_tree={zeros} "
        "post_porcelain_count=0 nonce=0"
    )
    assert _gate(repo, log, "/bin/sh", "-c", imitation) == 0
    lines = log.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:-1]))

    code, out = _verify(repo, log)

    assert code == 6
    assert "GATE_LOG_REFUSED LOG_TRUNCATED" in out


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "LOG_EMPTY"),
        ("not a gate log\n", "LOG_MALFORMED"),
        ("GATE-LOG v2\nnonce=short\n", "LOG_MALFORMED"),
        ("partial line without newline", "LOG_TRUNCATED"),
    ],
)
def test_a_malformed_log_is_refused(tmp_path: Path, text: str, reason: str) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "bad.log"
    log.write_text(text)

    code, out = _verify(repo, log)

    assert code == 6
    assert f"GATE_LOG_REFUSED {reason}" in out


def test_a_missing_log_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)

    code, out = _verify(repo, tmp_path / "absent.log")

    assert code == 6
    assert "GATE_LOG_REFUSED LOG_MISSING" in out


def test_a_porcelain_count_without_its_lines_is_refused(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "count.log"
    assert _gate(repo, log, "true") == 0
    log.write_text(log.read_text().replace("porcelain_count=0", "porcelain_count=1"))

    code, out = _verify(repo, log)

    assert code == 6
    assert "GATE_LOG_REFUSED LOG_MALFORMED porcelain_count=1" in out


@pytest.mark.parametrize(
    "argv",
    [[], ["--verify"], ["--verify", "x.log", "--pin"], ["--bogus"], ["only-a-log.log"]],
)
def test_a_usage_error_exits_2(tmp_path: Path, argv: list[str]) -> None:
    repo = new_repo(tmp_path)

    result = script("gate-log.sh", *argv, cwd=repo)

    assert result.returncode == 2
    assert "usage: gate-log.sh" in result.stderr


def test_an_unresolvable_pin_exits_2(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    log = tmp_path / "pin.log"
    assert _gate(repo, log, "true") == 0

    code, out = _verify(repo, log, "--pin", "no-such-revision")

    assert code == 2
    assert "GATE_LOG_REFUSED PIN_UNRESOLVED no-such-revision" in out


def test_an_absent_full_length_pin_exits_2_and_a_present_one_passes(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    log = tmp_path / "full-pin.log"
    assert _gate(repo, log, "true") == 0
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    absent = "0123456789abcdef0123456789abcdef01234567"
    assert absent != head

    absent_code, absent_out = _verify(repo, log, "--pin", absent)
    present_code, present_out = _verify(repo, log, "--pin", head)

    assert absent_code == 2
    assert f"GATE_LOG_REFUSED PIN_UNRESOLVED {absent}" in absent_out
    assert present_code == 0
    assert f"GATE_LOG_OK head={head}" in present_out
