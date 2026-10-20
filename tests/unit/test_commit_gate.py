# SPDX-License-Identifier: MIT-0
"""The commit-tier hooks refuse what the push gate would refuse, and judge what git will publish.

`.githooks/pre-commit` runs `scripts/commit_gate.py` on the index; `.githooks/pre-push` runs
`make publication-gate` with the BASE the pushed range builds on. A failure here means a commit
that carries a secret, local material, a denylisted literal, a foreign identity or a lint error
reaches history unannounced, or a push gated against the wrong range (or not gated at all).

Fixture repositories replace global and system git configuration with an empty file, so the
operator's hooks and identity never decide a test. The pre-push tests replace `make` with a stub
that records its arguments; the publication gate itself is tested in test_publication_gate.py.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import string
import sys
from pathlib import Path
from types import ModuleType

import pytest
from gate_script_support import BASH, SCRIPTS, git, run

pytestmark = pytest.mark.unit

HOOKS = SCRIPTS.parent / ".githooks"
EMAIL = "fixture@example.invalid"
OUTSIDER = "outsider@example.invalid"
# The RFC 4648 base32 alphabet, derived here independently of the gates under test.
BASE32 = string.ascii_uppercase + "234567"
DENYLISTED = "AcmeInternalTool"
GITIGNORE = "local-denylist.txt\nbuild/\n"
ZERO = "0" * 40
IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
)


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    """Load the commit gate as a module; it loads the publication gate beside it."""
    loaded = sys.modules.get("commit_gate")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("commit_gate", SCRIPTS / "commit_gate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pinned_gitleaks_installed(gate: ModuleType) -> bool:
    if shutil.which("gitleaks") is None:
        return False
    versions = gate.tool_versions
    return versions.mismatch("gitleaks", versions.installed_gitleaks()) is None


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository with one commit, an allowed identity and a denylist."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "fixture"),
        ("user.email", EMAIL),
        ("publication.allowedEmail", EMAIL),
        ("commit.gpgsign", "false"),
    ):
        git(path, "config", key, value)
    (path / ".gitignore").write_text(GITIGNORE)
    (path / "local-denylist.txt").write_text(f"# fixture denylist\n\n{DENYLISTED}\n")
    git(path, "add", ".gitignore")
    git(path, "commit", "-q", "-m", "fixture base")
    return path


def _stage(repo: Path, files: dict[str, str], *, force: bool = False) -> None:
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(repo, "add", *(["-f"] if force else []), *files)


def _gate(gate: ModuleType, repo: Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    return gate.run_gate(repo, out=lines.append), lines


def _refused(lines: list[str]) -> list[str]:
    """Return the per-check refusal lines, without the summary line."""
    summary = "commit-gate: REFUSED "
    return [line for line in lines if " REFUSED " in line and not line.startswith(summary)]


def _token() -> str:
    """Return an access-key-shaped token built at run time; no key is ever committed."""
    return "AKIA" + "".join(secrets.SystemRandom().sample(BASE32, 16))


# --------------------------------------------------------------------------------------------
# pre-commit: scripts/commit_gate.py
# --------------------------------------------------------------------------------------------


def test_a_clean_staged_change_passes_every_check(gate: ModuleType, repo: Path) -> None:
    if not _pinned_gitleaks_installed(gate):
        pytest.skip("the pinned gitleaks is not installed")
    _stage(repo, {"tests/unit/test_ok.py": '"""Fixture."""\n\nVALUE = 1\n', "notes.txt": "hi\n"})
    code, lines = _gate(gate, repo)
    assert code == 0, lines
    assert lines[-1] == "commit-gate: passed"
    assert _refused(lines) == []


def test_a_staged_secret_refuses_without_printing_it(gate: ModuleType, repo: Path) -> None:
    if not _pinned_gitleaks_installed(gate):
        pytest.skip("the pinned gitleaks is not installed")
    token = _token()
    _stage(repo, {"credentials.txt": f"aws_access_key_id = {token}\n"})
    code, lines = _gate(gate, repo)
    assert code == 1
    assert _refused(lines) == [
        "commit-gate: secret-staged REFUSED SECRET_STAGED "
        "aws-access-token at staged credentials.txt:1"
    ]
    assert token not in "\n".join(lines)


def test_an_unpinned_gitleaks_refuses(
    gate: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate.tool_versions, "installed_gitleaks", lambda: "0.0.1")
    [refusal] = gate.check_tool_version(repo)
    assert refusal.code == "TOOL_VERSION_MISMATCH"


@pytest.mark.parametrize(
    "path",
    [".agents/notes.md", "internal/plan", "Security-Findings.md", ".env.local", ".Runs/x"],
)
def test_a_staged_local_path_refuses(gate: ModuleType, repo: Path, path: str) -> None:
    _stage(repo, {path: "local\n"}, force=True)
    [refusal] = gate.check_forbidden_paths(repo)
    assert refusal.code == "FORBIDDEN_PATH"
    assert refusal.detail.startswith(f"staged {path}: ")


def test_unstaging_a_local_path_is_not_refused(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"internal/plan": "local\n"})
    git(repo, "commit", "-q", "-m", "fixture local")
    git(repo, "rm", "-q", "--cached", "internal/plan")
    assert gate.check_forbidden_paths(repo) == []


def test_a_force_added_ignored_path_refuses(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"build/out.txt": "artefact\n", "kept.txt": "kept\n"}, force=True)
    refusals = gate.check_staged_ignored(repo)
    assert [(r.code, r.detail) for r in refusals] == [
        ("TRACKED_IGNORED", "staged build/out.txt is ignored")
    ]


def test_a_denylisted_literal_refuses_by_line_number_only(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"notes.txt": f"uses {DENYLISTED.upper()} here\n"})
    code, lines = _gate(gate, repo)
    assert code == 1
    denied = [line for line in _refused(lines) if "DENYLIST_HIT" in line]
    assert denied == [
        "commit-gate: denylist REFUSED DENYLIST_HIT local-denylist.txt line 3 in staged notes.txt"
    ]
    assert DENYLISTED.casefold() not in "\n".join(lines).casefold()


def test_a_denylisted_path_is_withheld_in_every_refusal(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {f"internal/{DENYLISTED}.md": "x\n"})
    _code, lines = _gate(gate, repo)
    assert any("FORBIDDEN_PATH" in line and "[withheld]" in line for line in lines)
    assert DENYLISTED.casefold() not in "\n".join(lines).casefold()


def test_a_removed_denylisted_line_is_not_refused(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"notes.txt": f"{DENYLISTED}\nkeep\n"})
    git(repo, "commit", "-q", "-m", "fixture old")
    _stage(repo, {"notes.txt": "keep\n"})
    denylist = gate.publication_gate.load_denylist(repo)
    assert gate.check_denylist(repo, denylist) == []


def test_an_absent_denylist_warns_and_skips_the_check(gate: ModuleType, repo: Path) -> None:
    (repo / "local-denylist.txt").unlink()
    _stage(repo, {"notes.txt": f"{DENYLISTED}\n"})
    _code, lines = _gate(gate, repo)
    assert any(line.startswith("commit-gate: denylist WARNING ") for line in lines)
    assert not any("DENYLIST_HIT" in line for line in lines)


def test_an_outside_identity_refuses(
    gate: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", OUTSIDER)
    assert [(r.code, r.detail) for r in gate.check_identity(repo)] == [
        ("IDENTITY_NOT_ALLOWED", f"staged commit author {OUTSIDER}")
    ]


def test_no_configured_identity_refuses(gate: ModuleType, repo: Path) -> None:
    git(repo, "config", "--unset-all", "publication.allowedEmail")
    git(repo, "config", "--unset", "user.email")
    [refusal] = gate.check_identity(repo)
    assert refusal.code == "IDENTITY_NOT_ALLOWED"


def test_lint_judges_the_staged_content_not_the_working_tree(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"tests/test_bad.py": '"""Fixture."""\n\nimport os\n'})
    (repo / "tests/test_bad.py").write_text('"""Fixture."""\n')
    refusals = gate.check_lint(repo)
    assert [r.code for r in refusals] == ["LINT_FAILED"]
    assert refusals[0].detail.startswith("ruff check tests/test_bad.py: ")
    assert "F401" in refusals[0].detail


def test_lint_refuses_unformatted_staged_content(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"src/module.py": '"""Fixture."""\n\nx=1\n'})
    assert [(r.code, r.detail) for r in gate.check_lint(repo)] == [
        ("LINT_FAILED", "ruff format src/module.py: exited 1")
    ]


def test_lint_reads_only_the_make_lint_scope(gate: ModuleType, repo: Path) -> None:
    _stage(repo, {"scripts/tool.py": "import os\n", "docs/x.md": "import os\n"})
    assert gate.lint_targets(gate.staged_paths(repo)) == []
    assert gate.check_lint(repo) == []


# --------------------------------------------------------------------------------------------
# pre-push: .githooks/pre-push
# --------------------------------------------------------------------------------------------


@pytest.fixture
def pushed(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return the repository (origin/main at its base, `make` stubbed) and the stub's log."""
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _stage(repo, {"change.txt": "change\n"})
    git(repo, "commit", "-q", "-m", "fixture change")
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "make.log"
    make = stub / "make"
    make.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit "${{STUB_EXIT:-0}}"\n')
    make.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("STUB_EXIT", raising=False)
    return repo, log


def _push(repo: Path, *lines: str) -> tuple[int, str]:
    stdin = "".join(f"{line}\n" for line in lines)
    result = run(
        [BASH, str(HOOKS / "pre-push"), "origin", "https://example.invalid/repo.git"],
        repo,
        stdin=stdin,
    )
    return result.returncode, result.stderr


def _made(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_a_push_onto_an_existing_ref_gates_from_its_remote_tip(
    pushed: tuple[Path, Path],
) -> None:
    repo, log = pushed
    head, base = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "origin/main")
    code, err = _push(repo, f"refs/heads/main {head} refs/heads/main {base}")
    assert code == 0, err
    assert _made(log) == [f"publication-gate BASE={base}"]


def test_a_new_remote_ref_gates_from_remote_main(
    pushed: tuple[Path, Path],
) -> None:
    repo, log = pushed
    head, base = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "origin/main")
    code, err = _push(repo, f"refs/heads/topic {head} refs/heads/topic {ZERO}")
    assert code == 0, err
    assert _made(log) == [f"publication-gate BASE={base}"]


def test_a_refusing_gate_refuses_the_push(
    pushed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, log = pushed
    head, base = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "origin/main")
    monkeypatch.setenv("STUB_EXIT", "1")
    code, _err = _push(repo, f"refs/heads/main {head} refs/heads/main {base}")
    assert code != 0
    assert _made(log) == [f"publication-gate BASE={base}"]


def test_a_tag_is_refused_without_running_the_gate(
    pushed: tuple[Path, Path],
) -> None:
    repo, log = pushed
    head = git(repo, "rev-parse", "HEAD")
    code, err = _push(repo, f"refs/tags/v1 {head} refs/tags/v1 {ZERO}")
    assert code == 1
    assert "pre-push: REFUSED refs/tags/v1: tags are never pushed" in err
    assert _made(log) == []


def test_a_ref_other_than_head_is_refused(pushed: tuple[Path, Path]) -> None:
    repo, log = pushed
    base = git(repo, "rev-parse", "origin/main")
    code, err = _push(repo, f"refs/heads/old {base} refs/heads/old {ZERO}")
    assert code == 1
    assert "is not HEAD" in err
    assert _made(log) == []


def test_an_unknown_remote_tip_is_refused(pushed: tuple[Path, Path]) -> None:
    repo, log = pushed
    head = git(repo, "rev-parse", "HEAD")
    unknown = "1" * 40
    code, err = _push(repo, f"refs/heads/main {head} refs/heads/main {unknown}")
    assert code == 1
    assert "run git fetch origin" in err
    assert _made(log) == []


def test_a_deletion_publishes_nothing_and_runs_no_gate(
    pushed: tuple[Path, Path],
) -> None:
    repo, log = pushed
    base = git(repo, "rev-parse", "origin/main")
    code, err = _push(repo, f"(delete) {ZERO} refs/heads/topic {base}")
    assert code == 0, err
    assert _made(log) == []


def test_both_hooks_are_executable() -> None:
    for name in ("pre-commit", "pre-push"):
        assert os.access(HOOKS / name, os.X_OK), name
