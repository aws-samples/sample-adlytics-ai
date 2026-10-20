# SPDX-License-Identifier: MIT-0
"""make install-hooks installs the tracked hooks without touching git configuration.

`scripts/install-hooks.sh` writes one stub per `.githooks/<name>` into the repository's common
hooks directory. It must never set core.hooksPath at any scope: a system-level hooksPath set by the
machine's security tooling would otherwise stop applying to the repository. A failure here means
the installer changed git configuration, overwrote a hook it did not write, left a stub behind on
uninstall, or installed a stub that does not run the tracked hook.

Every test runs on a throwaway repository whose global configuration is an empty file and whose
system configuration is a file the test controls, so the operator's real hooks never run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gate_script_support import commit, git, new_repo, script

pytestmark = pytest.mark.unit

TRACKED = {
    # Each fixture hook leaves a trace of its name and arguments, then passes or refuses.
    "pre-commit": '#!/bin/sh\necho "pre-commit $*" >> "$(git rev-parse --git-common-dir)/trace"\n',
    "pre-push": '#!/bin/sh\necho "pre-push $*" >> "$(git rev-parse --git-common-dir)/trace"\n',
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository with two tracked hooks and a system-level hooksPath of its own."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    system_hooks = tmp_path / "system-hooks"
    system_hooks.mkdir()
    system = tmp_path / "system.gitconfig"
    system.write_text(f"[core]\n\thooksPath = {system_hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    path = new_repo(tmp_path)
    (path / ".githooks").mkdir()
    for name, text in TRACKED.items():
        commit(path, f".githooks/{name}", text)
        (path / ".githooks" / name).chmod(0o755)
    return path


def _hooks(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"


def _config(repo: Path) -> str:
    return git(repo, "config", "--list", "--show-origin")


def test_install_writes_stubs_into_the_common_hooks_dir_and_changes_no_config(repo: Path) -> None:
    before = _config(repo)

    result = script("install-hooks.sh", "install", cwd=repo)

    assert result.returncode == 0, result.stderr
    assert _config(repo) == before
    assert "core.hookspath" in before.casefold()
    for name in TRACKED:
        stub = _hooks(repo) / name
        assert stub.stat().st_mode & 0o111
        assert "installed by make install-hooks" in stub.read_text()
        assert f'/.githooks/{name}"' in stub.read_text()


def test_install_from_a_worktree_installs_into_the_shared_hooks_dir(
    repo: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))

    result = script("install-hooks.sh", "install", cwd=worktree)

    assert result.returncode == 0, result.stderr
    assert _hooks(worktree) == repo / ".git" / "hooks"
    assert {p.name for p in (repo / ".git" / "hooks").iterdir()} >= set(TRACKED)


def test_a_stub_runs_the_tracked_hook_of_its_checkout(repo: Path) -> None:
    script("install-hooks.sh", "install", cwd=repo)

    result = script(str(_hooks(repo) / "pre-push"), "origin", "file:///remote", cwd=repo)

    assert result.returncode == 0, result.stderr
    assert (repo / ".git" / "trace").read_text() == "pre-push origin file:///remote\n"


def test_install_is_idempotent(repo: Path) -> None:
    script("install-hooks.sh", "install", cwd=repo)
    first = {name: (_hooks(repo) / name).read_bytes() for name in TRACKED}

    again = script("install-hooks.sh", "install", cwd=repo)

    assert again.returncode == 0, again.stderr
    assert again.stdout.count("already installed") == len(TRACKED)
    assert {name: (_hooks(repo) / name).read_bytes() for name in TRACKED} == first


def test_a_conflicting_hook_is_refused_named_and_left_untouched(repo: Path) -> None:
    foreign = _hooks(repo) / "pre-push"
    foreign.write_text("#!/bin/sh\nexit 0\n")
    before = _config(repo)

    result = script("install-hooks.sh", "install", cwd=repo)

    assert result.returncode == 1
    assert f"REFUSED {foreign} exists" in result.stderr
    assert foreign.read_text() == "#!/bin/sh\nexit 0\n"
    # All or nothing: the hook without a conflict is not installed either.
    assert not (_hooks(repo) / "pre-commit").exists()
    assert _config(repo) == before


def test_uninstall_removes_only_its_own_stubs(repo: Path) -> None:
    script("install-hooks.sh", "install", cwd=repo)
    foreign = _hooks(repo) / "post-merge"
    foreign.write_text("#!/bin/sh\nexit 0\n")
    (_hooks(repo) / "pre-push").write_text("#!/bin/sh\necho edited\n")
    before = _config(repo)

    result = script("install-hooks.sh", "uninstall", cwd=repo)

    assert result.returncode == 0, result.stderr
    assert not (_hooks(repo) / "pre-commit").exists()
    assert (_hooks(repo) / "pre-push").read_text() == "#!/bin/sh\necho edited\n"
    assert "left " in result.stderr
    assert foreign.exists()
    assert _config(repo) == before


def test_a_real_commit_runs_the_installed_hook_when_no_system_hooks_path_is_set(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    script("install-hooks.sh", "install", cwd=repo)

    commit(repo, "b.txt", "second\n")

    assert (repo / ".git" / "trace").read_text() == "pre-commit \n"
