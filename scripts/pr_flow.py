#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Push the current feature branch to the public remote, behind the push gate.

    make publish [DRY_RUN=1]    (uv run python scripts/pr_flow.py publish [--dry-run])

The remote and its main come from the publication gate's DEFAULT_BASE (`public/main`). The
remote's URL is the operator's git configuration and never appears in this repository.

publish pushes HEAD to refs/heads/<current branch> on the remote with no tags and no force (no
`+` in the refspec, no force option), so the remote accepts only a fast-forward. It never pushes
main. It refuses with a named code, before anything leaves the clone:

    DIRTY_TREE                 uncommitted or untracked changes: the gates judged HEAD alone
    DETACHED_HEAD              HEAD is not a branch, so there is no branch to push
    ON_MAIN                    HEAD is main, which changes only through a merged pull request
    RELEASE_BRANCH             HEAD is a release/* branch, which is never pushed
    NO_REMOTE                  the remote is not configured
    NO_BASE                    <remote>/main is unknown after git fetch
    NOTHING_TO_PUBLISH         HEAD is <remote>/main itself
    NOT_BASED_ON_MAIN          the branch's history does not contain <remote>/main
    PUBLICATION_GATE_FAILED    make publication-gate BASE=<remote>/main refused or wrote no record
    DENYLIST_MISSING, DENYLIST_EMPTY, DENYLIST_HIT, IDENTITY_NOT_ALLOWED
                               the publication gate's own denylist and identity checks, run again
                               on the range: a reused gate record is no older than these checks
    CHECKOUT_CHANGED           HEAD or the working tree changed while the checks ran
    VERIFY_FAILED              after the push, the remote branch is not HEAD or the remote has tags

A passing, canonical, clean publication-gate record of this HEAD on this BASE is reused; without
one, publish runs the gate. --dry-run runs every read-only query and every check, and prints each
command that would change something (git fetch, make publication-gate, git push) instead of
running it. ADLYTICS_* and AWS_* are removed from the environment first: nothing here needs AWS.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
EXIT_REFUSED = 1
EXIT_USAGE = 2
SCRUBBED_PREFIXES = ("ADLYTICS_", "AWS_")
RELEASE_PREFIX = "release/"
PUBLICATION_KIND = "publication-gate"


def _sibling(name: str) -> ModuleType:
    """Load a script beside this one as a module, reusing an already loaded copy."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publication_gate = _sibling("publication_gate")
run_record = publication_gate.run_record


class RefusedError(Exception):
    """A guard refused; the code names it and the detail never holds a denylisted literal."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code} {detail}")
        self.code = code
        self.detail = detail


def scrubbed_environment(environ: dict[str, str]) -> dict[str, str]:
    """Return `environ` without ADLYTICS_* or AWS_* variables: no gate or git call needs AWS."""
    return {k: v for k, v in environ.items() if not k.startswith(SCRUBBED_PREFIXES)}


def remote_and_main() -> tuple[str, str]:
    """Return the remote and its main branch, both named by the publication gate's DEFAULT_BASE."""
    remote, _, main = str(publication_gate.DEFAULT_BASE).partition("/")
    return remote, main


@dataclass(frozen=True)
class Span:
    """The commits a push or merge carries: BASE (exclusive) to HEAD, and HEAD's tree."""

    base: str
    head: str
    tree: str


@dataclass
class Commands:
    """Runs read-only queries always, and changes only outside a dry run, printing each change."""

    root: Path
    dry_run: bool
    out: Callable[[str], None] = print

    def _run(self, argv: Sequence[str], *, capture: bool) -> subprocess.CompletedProcess[bytes]:
        try:
            # A-SUBPROCESS-001: fixed git, gh and make argv built in this module; see
            # docs/security-dispositions.md
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            return subprocess.run(  # noqa: S603
                list(argv), cwd=self.root, capture_output=capture, check=False
            )
        except OSError as error:
            raise RefusedError("TOOL_MISSING", f"{argv[0]} could not start: {error}") from error

    def status(self, *argv: str) -> int:
        """Run a read-only query and return its exit code alone."""
        return self._run(argv, capture=True).returncode

    def query(self, *argv: str) -> str:
        """Run a read-only query and return its stripped stdout, refusing when it fails."""
        result = self._run(argv, capture=True)
        if result.returncode != 0:
            lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
            last = lines[-1] if lines else "(no output)"
            detail = f"{argv[0]} {argv[1]} exited {result.returncode}: {last}"
            raise RefusedError("COMMAND_FAILED", detail)
        return result.stdout.decode("utf-8", "replace").strip()

    def change(self, *argv: str, code: str = "COMMAND_FAILED") -> bool:
        """Run a command that changes something, or print it in a dry run; True when it ran."""
        line = shlex.join(argv)
        if self.dry_run:
            self.out(f"dry-run: {line}")
            return False
        self.out(f"run: {line}")
        result = self._run(argv, capture=False)
        if result.returncode != 0:
            raise RefusedError(code, f"{line} exited {result.returncode}")
        return True


# --------------------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------------------


def require_clean(cmd: Commands) -> None:
    """Refuse uncommitted or untracked changes."""
    if cmd.query("git", "status", "--porcelain", "--untracked-files=all"):
        raise RefusedError(
            "DIRTY_TREE", "commit or remove every change first; the gates judged HEAD"
        )


def feature_branch(cmd: Commands, main: str) -> str:
    """Return HEAD's branch, refusing a detached HEAD, main and any release/* branch."""
    if cmd.status("git", "symbolic-ref", "-q", "HEAD") != 0:
        raise RefusedError("DETACHED_HEAD", "check out the feature branch to push")
    branch = cmd.query("git", "symbolic-ref", "--short", "HEAD")
    if branch == main:
        raise RefusedError(
            "ON_MAIN", f"{main} changes only by merging a pull request (make merge-pr)"
        )
    if branch.startswith(RELEASE_PREFIX):
        raise RefusedError("RELEASE_BRANCH", f"{branch} is a release branch, which is never pushed")
    return branch


def remote_tip(cmd: Commands, remote: str, main: str) -> str:
    """Return the commit of <remote>/<main> as the last fetch left it."""
    ref = f"refs/remotes/{remote}/{main}"
    if cmd.status("git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") != 0:
        raise RefusedError("NO_BASE", f"{remote}/{main} is unknown; run git fetch {remote}")
    return cmd.query("git", "rev-parse", "--verify", f"{ref}^{{commit}}")


def require_based_on(cmd: Commands, span: Span, label: str) -> None:
    """Refuse HEAD when it is BASE itself or its history does not contain BASE."""
    if span.head == span.base:
        raise RefusedError("NOTHING_TO_PUBLISH", f"HEAD is already {label}")
    if cmd.status("git", "merge-base", "--is-ancestor", span.base, span.head) != 0:
        raise RefusedError(
            "NOT_BASED_ON_MAIN",
            f"the branch's history does not contain {label} {span.base[:12]}; integrate it first",
        )


def head_span(cmd: Commands, base: str) -> Span:
    """Return BASE with HEAD's commit and tree."""
    head = cmd.query("git", "rev-parse", "--verify", "HEAD^{commit}")
    return Span(base, head, cmd.query("git", "rev-parse", "HEAD^{tree}"))


def range_refusals(cmd: Commands, span: Span) -> list[RefusedError]:
    """Run the publication gate's denylist and identity checks over BASE..HEAD."""
    with tempfile.TemporaryDirectory(prefix="pr-flow-") as work:
        ctx = publication_gate.Context(
            cmd.root,
            span.base,
            span.head,
            span.tree,
            run_record.record_root(None, cwd=cmd.root),
            Path(work),
            publication_gate.load_denylist(cmd.root),
        )
        found = [*publication_gate.check_denylist(ctx), *publication_gate.check_identity(ctx)]
    return [RefusedError(r.code, r.detail) for r in found]


def publication_record(cmd: Commands, span: Span) -> Path | None:
    """Return the newest passing, canonical, clean publication-gate record of HEAD on BASE."""
    runs = run_record.record_root(None, cwd=cmd.root)
    for directory in run_record.find_records(
        runs, PUBLICATION_KIND, tree=span.tree, passing=True, canonical=True, clean=True
    ):
        run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        if isinstance(run, dict) and run.get("head") == span.head and run.get("base") == span.base:
            return directory
    return None


def relative(cmd: Commands, path: Path) -> str:
    """Return a record path relative to the checkout, so output names no local directory."""
    try:
        return path.resolve().relative_to(cmd.root.resolve()).as_posix()
    except ValueError:
        return path.name


# --------------------------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------------------------


def publish(cmd: Commands, remote: str, main: str) -> None:
    """Push HEAD to its own branch on the remote after every guard and the push gate pass."""
    require_clean(cmd)
    branch = feature_branch(cmd, main)
    if cmd.status("git", "remote", "get-url", remote) != 0:
        raise RefusedError("NO_REMOTE", f"the remote {remote!r} is not configured")
    cmd.change("git", "fetch", "--quiet", "--no-tags", remote)
    span = head_span(cmd, remote_tip(cmd, remote, main))
    head = span.head
    require_based_on(cmd, span, f"{remote}/{main}")
    cmd.out(f"publish: {branch} at {head[:12]} (tree {span.tree[:12]}) builds on {remote}/{main}")

    record = publication_record(cmd, span)
    if record is None:
        cmd.out(f"publish: no passing publication-gate record of HEAD on BASE {span.base[:12]}")
        ran = cmd.change(
            "make",
            "--no-print-directory",
            "publication-gate",
            f"BASE={span.base}",
            code="PUBLICATION_GATE_FAILED",
        )
        if ran:
            record = publication_record(cmd, span)
            if record is None:
                raise RefusedError("PUBLICATION_GATE_FAILED", "the gate wrote no passing record")
    if record is not None:
        cmd.out(f"publish: publication-gate {relative(cmd, record)}")
    else:
        cmd.out("publish: dry-run: the push needs that run's passing record")

    refusals = range_refusals(cmd, span)
    for refusal in refusals:
        cmd.out(f"publish: REFUSED {refusal.code} {refusal.detail}")
    if refusals:
        codes = sorted({r.code for r in refusals})
        raise RefusedError(codes[0], f"{len(refusals)} finding(s): {' '.join(codes)}")
    cmd.out("publish: denylist and identity ok")

    if cmd.query("git", "rev-parse", "HEAD") != head:
        raise RefusedError("CHECKOUT_CHANGED", "HEAD moved while the checks ran")
    require_clean_after(cmd)

    # No '+' in the refspec and no force option: the remote accepts only a fast-forward. No tags.
    if not cmd.change("git", "push", "--no-follow-tags", remote, f"HEAD:refs/heads/{branch}"):
        return
    listed = cmd.query("git", "ls-remote", remote, f"refs/heads/{branch}").split()
    if not listed or listed[0] != head:
        raise RefusedError(
            "VERIFY_FAILED", f"{remote}'s {branch} is not {head[:12]} after the push"
        )
    if cmd.query("git", "ls-remote", "--tags", remote):
        raise RefusedError("VERIFY_FAILED", f"{remote} has tags")
    cmd.out(f"publish: pushed {head[:12]} to {remote} {branch}; the remote has no tags")


def require_clean_after(cmd: Commands) -> None:
    """Refuse a working tree that changed while the checks ran."""
    if cmd.query("git", "status", "--porcelain", "--untracked-files=all"):
        raise RefusedError("CHECKOUT_CHANGED", "the working tree changed while the checks ran")


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse the subcommand and run it on the repository containing the working directory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    publish_parser = sub.add_parser("publish", help="push the current feature branch")
    publish_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    kept = scrubbed_environment(dict(os.environ))
    os.environ.clear()
    os.environ.update(kept)
    top = run_record.toplevel(Path.cwd())
    if top is None:
        sys.stderr.write(f"{args.command}: not in a git repository\n")
        return EXIT_USAGE
    cmd = Commands(top, dry_run=args.dry_run)
    remote, main_branch = remote_and_main()
    try:
        publish(cmd, remote, main_branch)
    except RefusedError as refusal:
        sys.stderr.write(f"{args.command}: REFUSED {refusal.code} {refusal.detail}\n")
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
