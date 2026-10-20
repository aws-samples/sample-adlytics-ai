# SPDX-License-Identifier: MIT-0
"""A throwaway git repository and a runner for the repository's bash gate scripts.

The gate-log and review-entry scripts read git state from their working directory, so their tests
run them inside a fresh `git init` repository under `tmp_path`. Commits pass identity with `-c`
so no git configuration is written anywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BASH = "/bin/bash"
_GIT = shutil.which("git")


def git_path() -> str:
    """Return the absolute git executable, failing loudly when git is absent."""
    if _GIT is None:
        raise RuntimeError("git is not on PATH; the gate-script tests need it")
    return _GIT


def run(
    argv: list[str], cwd: Path, *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv with no shell, feeding `stdin` when given, and return its process."""
    # A-SUBPROCESS-001: fixed interpreter and argv; see docs/security-dispositions.md
    # The argv is bash or git plus a repository-owned script and test constants; no caller input.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(  # noqa: S603
        argv, cwd=cwd, input=stdin, capture_output=True, text=True, check=False
    )


def script(name: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `scripts/<name>` under bash in `cwd`."""
    return run([BASH, str(SCRIPTS / name), *args], cwd)


def git(repo: Path, *args: str) -> str:
    """Run git in `repo`, raising on failure, and return its stripped stdout."""
    result = run([git_path(), *args], repo)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def commit(repo: Path, name: str, text: str) -> str:
    """Write one file, commit it with a fixture identity, and return the new HEAD."""
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        f"fixture {name}",
    )
    return git(repo, "rev-parse", "HEAD")


def new_repo(tmp_path: Path) -> Path:
    """Create a repository on branch `main` with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "a.txt", "first\n")
    return repo
