# SPDX-License-Identifier: MIT-0
"""The pull-request flow pushes only a gated feature branch and refuses everything else.

`scripts/pr_flow.py` is the only push path to the public remote (`make publish`). A failure here
means a push that bypasses a guard: main pushed directly, a detached or release branch pushed, a
branch that is not based on the current public main, a push without a passing push gate, a tag,
or a force.

`make pr` opens the pull request whose body reports the gate records. A failure there means a
pull request for a branch that was never pushed, or a title or body carrying a denylisted name to
a public page.

No test reaches GitHub or pushes anywhere. Each builds a throwaway repository whose `public` remote
is a GitHub-shaped URL that a repository-local `insteadOf` rewrites to a bare repository under
`tmp_path`; the bare repository is filled by fetching into it, never by a push. Every run that
could push is a refusal before the push or a dry run, and each test then asserts that the bare
repository holds no branch it did not hold before. `gh` is a stub first on PATH that logs each
call, serves canned JSON for `pr list` and `pr view`, and fails every other call, with an empty
GH_CONFIG_DIR so no real login could be used. Global and system git configuration are replaced
with an empty file, so no operator hook, identity or URL rewrite decides a test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from gate_script_support import SCRIPTS, commit, git, run

pytestmark = pytest.mark.unit

EMAIL = "fixture@example.invalid"
OUTSIDER = "outsider@example.invalid"
DENYLISTED = "AcmeInternalTool"
DENYLIST = f"# fixture entries\n{DENYLISTED}\n"
GITIGNORE = ".runs/\nlocal-denylist.txt\n"
URL = "https://github.com/example/fixture.git"
SLUG = "example/fixture"
BRANCH = "feat/fixture"
# Logs every call; serves list.json and view.json; any other call fails unless merge.ok exists.
GH_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$GH_STUB_DIR/calls.log"
case "$1 $2" in
  "pr list") cat "$GH_STUB_DIR/list.json" ;;
  "pr view") cat "$GH_STUB_DIR/view.json" ;;
  "pr merge") [ -f "$GH_STUB_DIR/merge.ok" ] || exit 3 ;;
  *) echo "gh stub: unexpected call: $*" >&2; exit 3 ;;
esac
"""


def _load(name: str) -> ModuleType:
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def flow() -> ModuleType:
    """Load the flow script as a module; it loads the publication gate and recorder beside it."""
    return _load("pr_flow")


def _make_writable(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        Path(current).chmod(0o755)
        for name in dirs:
            (Path(current) / name).chmod(0o755)
        for name in files:
            (Path(current) / name).chmod(0o644)


class Checkout:
    """A fixture checkout on a feature branch, its bare `public` remote, and a stub gh."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "repo"
        self.path.mkdir()
        self.bare = tmp_path / "public.git"
        self.gh = tmp_path / "gh"
        (self.gh / "bin").mkdir(parents=True)
        (self.gh / "bin" / "gh").write_text(GH_STUB)
        (self.gh / "bin" / "gh").chmod(0o755)
        (self.gh / "config").mkdir()
        self.serve("list.json", [])
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "config", "publication.allowedEmail", EMAIL)
        commit(self.path, ".gitignore", GITIGNORE)
        (self.path / "local-denylist.txt").write_text(DENYLIST)
        git(tmp_path, "clone", "-q", "--bare", str(self.path), str(self.bare))
        git(self.path, "remote", "add", "public", URL)
        git(self.path, "config", f"url.{self.bare.as_uri()}.insteadOf", URL)
        git(self.path, "fetch", "-q", "public")
        git(self.path, "checkout", "-q", "-b", BRANCH)
        self.commit("feat: add the fixture feature", name="feature.txt")

    def commit(self, message: str, *, name: str = "change.txt", email: str = EMAIL) -> str:
        """Write `message` to one file, commit it with a fixture identity; return the new HEAD."""
        (self.path / name).write_text(f"{message}\n")
        git(self.path, "add", name)
        git(
            self.path,
            "-c",
            "user.name=fixture",
            "-c",
            f"user.email={email}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            message,
        )
        return self.head

    def serve(self, name: str, data: object) -> None:
        """Make the gh stub answer with `data` as JSON."""
        (self.gh / name).write_text(json.dumps(data))

    def gh_calls(self) -> list[str]:
        """Every call the gh stub received, in order."""
        log = self.gh / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def push_branch(self) -> None:
        """Give the bare remote HEAD's branch by fetching into it: a push in effect, not in fact."""
        git(self.bare, "fetch", "-q", str(self.path), f"{BRANCH}:{BRANCH}")

    @property
    def head(self) -> str:
        """HEAD's commit."""
        return git(self.path, "rev-parse", "HEAD")

    @property
    def tree(self) -> str:
        """HEAD's tree, which gate records are matched against."""
        return git(self.path, "rev-parse", "HEAD^{tree}")

    @property
    def tip(self) -> str:
        """The fixture's public/main as its last fetch left it."""
        return git(self.path, "rev-parse", "refs/remotes/public/main")

    def remote_branches(self) -> set[str]:
        """Every branch the bare remote holds."""
        listed = git(self.bare, "for-each-ref", "--format=%(refname)", "refs/heads")
        return set(listed.split())

    def remote_tags(self) -> str:
        """Every tag the bare remote holds."""
        return git(self.bare, "for-each-ref", "refs/tags")

    def record(self, flow: ModuleType, kind: str, **fields: Any) -> Path:
        """Write a sealed record of `kind` for HEAD's tree, passing unless fields say otherwise."""
        run_fields: dict[str, Any] = {
            "canonical": True,
            "clean": True,
            "tree": self.tree,
            "exit_code": 0,
            "report_missing": False,
            **fields,
        }
        step = {"name": kind, "exit_code": run_fields["exit_code"], "duration_s": 0.0}
        return flow.run_record.write_record(
            kind, run_fields, steps=[step], root=self.path / ".runs"
        )


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Checkout]:
    """Yield an isolated fixture checkout; read-only records are made writable for cleanup."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("RUN_RECORD_ROOT", raising=False)
    fixture = Checkout(tmp_path)
    monkeypatch.setenv("PATH", f"{fixture.gh / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_STUB_DIR", str(fixture.gh))
    monkeypatch.setenv("GH_CONFIG_DIR", str(fixture.gh / "config"))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    yield fixture
    _make_writable(fixture.path / ".runs")


def _flow(checkout: Checkout, *args: str) -> tuple[int, str]:
    """Run scripts/pr_flow.py in the checkout; return its exit code and stdout plus stderr."""
    result = run([sys.executable, str(SCRIPTS / "pr_flow.py"), *args], checkout.path)
    return result.returncode, result.stdout + result.stderr


def _publication_record(flow: ModuleType, checkout: Checkout) -> Path:
    return checkout.record(flow, "publication-gate", head=checkout.head, base=checkout.tip)


# --------------------------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------------------------


def test_publish_dry_run_prints_a_feature_branch_push_with_no_tags_and_no_force(
    flow: ModuleType, checkout: Checkout
) -> None:
    record = _publication_record(flow, checkout)
    before = checkout.remote_branches()
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 0, output
    assert "dry-run: git fetch --quiet --no-tags public" in output
    assert f"dry-run: git push --no-follow-tags public HEAD:refs/heads/{BRANCH}" in output
    assert f"publish: publication-gate .runs/publication-gate/{record.name}" in output
    assert "--force" not in output
    assert " +HEAD" not in output
    assert "make --no-print-directory publication-gate" not in output
    assert checkout.remote_branches() == before == {"refs/heads/main"}
    assert checkout.remote_tags() == ""


def test_publish_dry_run_without_a_gate_record_prints_the_gate_it_would_run(
    checkout: Checkout,
) -> None:
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 0, output
    assert f"dry-run: make --no-print-directory publication-gate BASE={checkout.tip}" in output
    assert "the push needs that run's passing record" in output
    assert checkout.remote_branches() == {"refs/heads/main"}


def test_publish_refuses_a_record_of_another_base(flow: ModuleType, checkout: Checkout) -> None:
    checkout.record(flow, "publication-gate", head=checkout.head, base="0" * 40)
    _, output = _flow(checkout, "publish", "--dry-run")
    assert "dry-run: make --no-print-directory publication-gate" in output


def test_publish_runs_the_gate_when_no_record_exists_and_refuses_when_it_fails(
    checkout: Checkout,
) -> None:
    # Not a dry run: the fixture has no Makefile, so the gate fails and nothing is pushed.
    code, output = _flow(checkout, "publish")
    assert code == 1
    assert "publish: REFUSED PUBLICATION_GATE_FAILED" in output
    assert "git push" not in output
    assert checkout.remote_branches() == {"refs/heads/main"}


def test_publish_refuses_main(checkout: Checkout) -> None:
    git(checkout.path, "checkout", "-q", "main")
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED ON_MAIN" in output
    assert "git push" not in output


def test_publish_refuses_a_detached_head(checkout: Checkout) -> None:
    git(checkout.path, "checkout", "-q", "--detach")
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED DETACHED_HEAD" in output


def test_publish_refuses_a_release_branch(checkout: Checkout) -> None:
    git(checkout.path, "checkout", "-q", "-b", "release/2026-10-21")
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED RELEASE_BRANCH" in output


def test_publish_refuses_a_branch_not_based_on_public_main(
    flow: ModuleType, checkout: Checkout
) -> None:
    # public/main moves on in the remote; the branch does not contain the new tip.
    git(checkout.path, "checkout", "-q", "main")
    newer = checkout.commit("fix: move main on", name="main.txt")
    git(checkout.bare, "fetch", "-q", str(checkout.path), "main:main")
    git(checkout.path, "checkout", "-q", BRANCH)
    git(checkout.path, "update-ref", "refs/remotes/public/main", newer)
    _publication_record(flow, checkout)
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED NOT_BASED_ON_MAIN" in output
    assert "git push" not in output


def test_publish_refuses_a_branch_that_is_public_main(checkout: Checkout) -> None:
    git(checkout.path, "checkout", "-q", "-b", "feat/empty", "main")
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED NOTHING_TO_PUBLISH" in output


def test_publish_refuses_a_dirty_tree(checkout: Checkout) -> None:
    (checkout.path / "stray.txt").write_text("stray\n")
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED DIRTY_TREE" in output


def test_publish_refuses_without_the_remote(checkout: Checkout) -> None:
    git(checkout.path, "remote", "remove", "public")
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED NO_REMOTE" in output


def test_publish_refuses_a_denylisted_message_without_printing_the_literal(
    flow: ModuleType, checkout: Checkout
) -> None:
    checkout.commit(f"feat: wire {DENYLISTED} in")
    _publication_record(flow, checkout)
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "publish: REFUSED DENYLIST_HIT local-denylist.txt line 2" in output
    assert DENYLISTED.casefold() not in output.casefold()
    assert "git push" not in output


def test_publish_refuses_an_absent_denylist(flow: ModuleType, checkout: Checkout) -> None:
    (checkout.path / "local-denylist.txt").unlink()
    _publication_record(flow, checkout)
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "REFUSED DENYLIST_MISSING" in output


def test_publish_refuses_an_outsider_identity(flow: ModuleType, checkout: Checkout) -> None:
    checkout.commit("feat: more", email=OUTSIDER)
    _publication_record(flow, checkout)
    code, output = _flow(checkout, "publish", "--dry-run")
    assert code == 1
    assert "REFUSED IDENTITY_NOT_ALLOWED" in output
    assert "git push" not in output


def test_publish_scrubs_aws_and_deploy_variables(flow: ModuleType) -> None:
    kept = flow.scrubbed_environment(
        {"AWS_PROFILE": "x", "ADLYTICS_ENV": "dev", "PATH": "/bin", "HOME": "/home"}
    )
    assert kept == {"PATH": "/bin", "HOME": "/home"}


def test_the_make_target_is_the_only_push_path() -> None:
    makefile = (SCRIPTS.parent / "Makefile").read_text()
    recipe = makefile.split("\npublish:", 1)[1].split("\n\n", 1)[0]
    assert "scripts/pr_flow.py publish" in recipe
    assert "local-publish.sh" not in makefile
    assert "--force" not in (SCRIPTS / "pr_flow.py").read_text()


# --------------------------------------------------------------------------------------------
# pr
# --------------------------------------------------------------------------------------------


def _create_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("dry-run: gh pr create")]
    assert len(lines) == 1, output
    return lines[0]


def test_pr_dry_run_prints_the_create_with_the_branch_summary_and_gate_records(
    flow: ModuleType, checkout: Checkout
) -> None:
    checkout.push_branch()
    record = _publication_record(flow, checkout)
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 0, output
    create = _create_line(output)
    assert f"--repo {SLUG} --base main --head {BRANCH}" in create
    assert "--title 'feat: add the fixture feature'" in create
    assert "--body-file " in create
    assert f"pr: | - `{checkout.head[:12]}` feat: add the fixture feature" in output
    assert f"| `make publication-gate` | pass | `.runs/publication-gate/{record.name}` |" in output
    assert "| `make release-gate` | not run on this tree | |" in output
    assert str(checkout.path) not in output
    # Only the read-only lookup reached the stub: the create was printed, not run.
    assert checkout.gh_calls() == [
        f"pr list --repo {SLUG} --head {BRANCH} --state open --json number,url"
    ]


def test_pr_dry_run_edits_the_open_pull_request(checkout: Checkout) -> None:
    checkout.push_branch()
    checkout.serve("list.json", [{"number": 7, "url": "https://github.com/example/fixture/pull/7"}])
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 0, output
    assert f"dry-run: gh pr edit 7 --repo {SLUG} --title" in output
    assert "gh pr create" not in output
    assert [call.split()[:2] for call in checkout.gh_calls()] == [["pr", "list"]]


def test_pr_titles_a_branch_of_several_commits_from_its_name(checkout: Checkout) -> None:
    checkout.commit("test: cover the fixture feature")
    checkout.push_branch()
    _, output = _flow(checkout, "pr", "--dry-run")
    assert "--title 'feat: fixture'" in _create_line(output)
    _, output = _flow(checkout, "pr", "--dry-run", "--title", "feat: ship the fixture")
    assert "--title 'feat: ship the fixture'" in _create_line(output)


def test_pr_refuses_a_branch_that_is_not_pushed(checkout: Checkout) -> None:
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 1
    assert "pr: REFUSED NOT_PUSHED" in output
    assert checkout.gh_calls() == []


def test_pr_refuses_a_pushed_branch_behind_head(checkout: Checkout) -> None:
    checkout.push_branch()
    checkout.commit("feat: a later change")
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 1
    assert "pr: REFUSED NOT_PUSHED" in output


def test_pr_refuses_main(checkout: Checkout) -> None:
    git(checkout.path, "checkout", "-q", "main")
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 1
    assert "pr: REFUSED ON_MAIN" in output


def test_pr_refuses_a_remote_that_is_not_github(checkout: Checkout) -> None:
    git(checkout.path, "remote", "set-url", "public", checkout.bare.as_uri())
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 1
    assert "pr: REFUSED REMOTE_NOT_GITHUB" in output
    assert checkout.bare.as_uri() not in output


def test_pr_refuses_a_denylisted_title_without_printing_it(checkout: Checkout) -> None:
    checkout.push_branch()
    code, output = _flow(checkout, "pr", "--dry-run", "--title", f"feat: {DENYLISTED}")
    assert code == 1
    assert "pr: REFUSED DENYLIST_HIT local-denylist.txt line 2 in the title" in output
    assert DENYLISTED.casefold() not in output.casefold()
    assert checkout.gh_calls() == []


def test_pr_refuses_a_denylisted_commit_subject_in_the_body(checkout: Checkout) -> None:
    checkout.commit(f"fix: drop {DENYLISTED}")
    checkout.push_branch()
    code, output = _flow(checkout, "pr", "--dry-run", "--title", "fix: tidy")
    assert code == 1
    assert "REFUSED DENYLIST_HIT local-denylist.txt line 2 in the body" in output
    assert DENYLISTED.casefold() not in output.casefold()


def test_pr_refuses_an_empty_denylist(checkout: Checkout) -> None:
    checkout.push_branch()
    (checkout.path / "local-denylist.txt").write_text("# no entries\n")
    code, output = _flow(checkout, "pr", "--dry-run")
    assert code == 1
    assert "REFUSED DENYLIST_EMPTY" in output
