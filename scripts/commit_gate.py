#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Refuse a commit whose staged changes the push gate would refuse, in seconds rather than minutes.

    uv run python scripts/commit_gate.py    (run by .githooks/pre-commit; `make install-hooks`)

The commit tier of the publication checks. It reads the index, not the working tree, so a partly
staged file is judged by what the commit will hold. Every check runs and refuses with a named code:
    TOOL_VERSION_MISMATCH  gitleaks is absent or not the CONTRIBUTING.md Maintainer tools pin
    SECRET_STAGED          gitleaks found a secret in the staged changes
    FORBIDDEN_PATH         a staged addition or change to local-only material (the publication
                           gate's forbidden_reason decides which paths)
    TRACKED_IGNORED        a staged addition or change to a path the ignore rules exclude
    DENYLIST_HIT           a local-denylist.txt literal in an added line or header of the staged
                           diff; the output names the denylist line, never the literal
    IDENTITY_NOT_ALLOWED   the author or committer email git will record is outside
                           publication.allowedEmail (git config; falls back to user.email)
    LINT_FAILED            ruff check or ruff format --check refuses a staged .py file under src/
                           or tests/, the scope of `make lint` and `make check-format`; a path
                           pyproject's [tool.ruff.format] exclude names is linted, not formatted

An absent or empty local-denylist.txt prints a warning here; the push tier owns that refusal.
`make publication-gate` stays the enforcing gate before every push: this hook is opt-in, and
`git commit --no-verify` skips it. Exit 0 only when nothing is refused. Writes no run record and
touches no AWS.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from fnmatch import fnmatch
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
# The scope of `make lint` and `make check-format` (`ruff ... src tests`).
LINT_ROOTS = ("src/", "tests/")
LINT_SUFFIXES = (".py", ".pyi")
EXIT_REFUSED = 1


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


# The publication gate owns every rule this tier shares with it: the path rules, the denylist
# format, the identity allowlist, the gitleaks invocation and the redaction.
publication_gate = _sibling("publication_gate")
tool_versions = publication_gate.tool_versions
Refusal = publication_gate.Refusal
GateError = publication_gate.GateError


def staged_paths(root: Path) -> list[str]:
    """Return every path the index adds, copies, modifies, renames or retypes against HEAD."""
    changed = publication_gate.git_bytes(
        root, "diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=ACMRT"
    )
    return sorted(set(publication_gate._nul_paths(changed)))


def check_tool_version(_root: Path) -> list[Refusal]:
    """Refuse an absent or unpinned gitleaks: a different version may miss what the pin finds."""
    try:
        problem = tool_versions.mismatch(
            "gitleaks", tool_versions.installed_gitleaks(), publication_gate.CONTRIBUTING
        )
    except tool_versions.ToolVersionError as error:
        problem = str(error)
    return [Refusal("TOOL_VERSION_MISMATCH", problem)] if problem else []


def check_secret_staged(root: Path) -> list[Refusal]:
    """Scan the staged diff with gitleaks, using the publication gate's invocation."""
    work = Path(tempfile.mkdtemp(prefix="commit-gate-"))
    try:
        ctx = publication_gate.Context(root, "", "", "", work, work)
        findings = publication_gate.gitleaks(
            ctx, ["git", "--pre-commit", "--staged", str(root)], "staged"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return [
        Refusal("SECRET_STAGED", publication_gate._finding(f, f"staged {f.get('File', '?')}"))
        for f in findings
    ]


def check_forbidden_paths(root: Path) -> list[Refusal]:
    """Refuse a staged addition or change to a path the publication gate would refuse."""
    refusals: list[Refusal] = []
    for path in staged_paths(root):
        reason = publication_gate.forbidden_reason(path)
        if reason is not None:
            refusals.append(Refusal("FORBIDDEN_PATH", f"staged {path}: {reason}"))
    return refusals


def check_staged_ignored(root: Path) -> list[Refusal]:
    """Refuse a staged addition or change to a path the ignore rules exclude."""
    paths = staged_paths(root)
    if not paths:
        return []
    listed = "".join(f"{path}\0" for path in paths).encode("utf-8", "surrogateescape")
    result = publication_gate._run(
        ["git", "check-ignore", "-z", "--no-index", "--stdin"], root, stdin=listed
    )
    if result.returncode not in {0, 1}:
        raise GateError(f"git check-ignore failed: {publication_gate._last_line(result.stderr)}")
    return [
        Refusal("TRACKED_IGNORED", f"staged {path} is ignored")
        for path in publication_gate._nul_paths(result.stdout)
    ]


def staged_diff_sources(root: Path) -> list[tuple[str, str]]:
    """Return (where, text) for every added line and header of the staged diff."""
    patch = publication_gate.git_bytes(
        root, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv"
    ).decode("utf-8", "replace")
    sources: list[tuple[str, str]] = []
    path = "?"
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1]
        # Removed and context lines are not new content; headers and added lines are.
        if line.startswith(("-", " ")) or not line:
            continue
        sources.append((f"staged {path}", line))
    return sources


def check_denylist(root: Path, denylist: Sequence[tuple[int, str]]) -> list[Refusal]:
    """Refuse any local-denylist.txt literal in the staged diff, naming only its line number."""
    refusals: list[Refusal] = []
    seen: set[tuple[int, str]] = set()
    for where, text in staged_diff_sources(root):
        folded = text.casefold()
        for number, literal in denylist:
            if literal in folded and (number, where) not in seen:
                seen.add((number, where))
                refusals.append(
                    Refusal("DENYLIST_HIT", f"{publication_gate.DENYLIST} line {number} in {where}")
                )
    return refusals


def _ident_email(root: Path, variable: str) -> str:
    ident = publication_gate.git_text(root, "var", variable)
    start, end = ident.rfind("<"), ident.rfind(">")
    if start < 0 or end < start:
        raise GateError(f"git var {variable} has no email")
    return ident[start + 1 : end]


def check_identity(root: Path) -> list[Refusal]:
    """Refuse the author or committer email this commit would record, if it is not allowed."""
    allowed = publication_gate.allowed_emails(root)
    if not allowed:
        key = publication_gate.ALLOWED_EMAIL_KEY
        return [
            Refusal("IDENTITY_NOT_ALLOWED", f"neither {key} nor user.email is set in git config")
        ]
    refusals: list[Refusal] = []
    for role, variable in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        email = _ident_email(root, variable)
        if email.casefold() not in allowed:
            refusals.append(Refusal("IDENTITY_NOT_ALLOWED", f"staged commit {role} {email}"))
    return refusals


def lint_targets(paths: Sequence[str]) -> list[str]:
    """Return the staged paths `make lint` would read."""
    return [p for p in paths if p.startswith(LINT_ROOTS) and p.endswith(LINT_SUFFIXES)]


def format_excluded(root: Path) -> tuple[str, ...]:
    """Return pyproject's `[tool.ruff.format]` exclude patterns, the generated modules.

    `make check-format` honours them, but ruff ignores them for content read from stdin even
    with `--force-exclude`, so without this every commit of a regenerated module is refused.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return ()
    tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
    section = tool.get("ruff", {}).get("format", {})
    return tuple(str(pattern) for pattern in section.get("exclude", ()))


def check_lint(root: Path) -> list[Refusal]:
    """Run ruff check and ruff format --check on the staged content of each lintable file."""
    refusals: list[Refusal] = []
    excluded = format_excluded(root)
    for path in lint_targets(staged_paths(root)):
        content = publication_gate.git_bytes(root, "show", f":{path}")
        for tool in (["check", "--output-format", "concise"], ["format", "--check"]):
            if tool[0] == "format" and any(fnmatch(path, pattern) for pattern in excluded):
                continue
            argv = [sys.executable, "-m", "ruff", *tool, "--force-exclude"]
            result = publication_gate._run(
                [*argv, "--stdin-filename", path, "-"], root, stdin=content
            )
            if result.returncode != 0:
                # ruff format --check reads stdin silently; ruff check prints one line per finding.
                output = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
                first = output.splitlines()[0] if output else f"exited {result.returncode}"
                refusals.append(Refusal("LINT_FAILED", f"ruff {tool[0]} {path}: {first}"))
    return refusals


Check = tuple[str, str, Callable[[Path], list[Refusal]]]


def run_gate(root: Path, *, out: Callable[[str], None] = print) -> int:
    """Run every check on the index, print one line per result, and return the exit code."""
    denylist = publication_gate.load_denylist(root)
    checks: list[Check] = [
        ("tool-version", "TOOL_VERSION_MISMATCH", check_tool_version),
        ("secret-staged", "SECRET_STAGED", check_secret_staged),
        ("forbidden-path", "FORBIDDEN_PATH", check_forbidden_paths),
        ("staged-ignored", "TRACKED_IGNORED", check_staged_ignored),
        ("identity", "IDENTITY_NOT_ALLOWED", check_identity),
        ("lint", "LINT_FAILED", check_lint),
    ]
    if denylist:
        checks.insert(4, ("denylist", "DENYLIST_HIT", lambda r: check_denylist(r, denylist)))
    else:
        out(
            f"commit-gate: denylist WARNING {publication_gate.DENYLIST} is absent or has no "
            "entries; the staged diff was not checked against it"
        )
    refused: set[str] = set()
    for name, code, run in checks:
        try:
            refusals = run(root)
        except (GateError, OSError, ValueError, UnicodeError) as error:
            refusals = [Refusal(code, f"the check could not run: {error}")]
        if not refusals:
            out(f"commit-gate: {name} ok")
        for refusal in refusals:
            detail = publication_gate.redact(refusal.detail, denylist)
            out(f"commit-gate: {name} REFUSED {refusal.code} {detail}")
            refused.add(refusal.code)
    if refused:
        out(f"commit-gate: REFUSED {' '.join(sorted(refused))}")
        return EXIT_REFUSED
    out("commit-gate: passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the commit gate on the repository containing the working directory."""
    if argv:
        sys.stderr.write("usage: commit_gate.py (no arguments)\n")
        return 2
    top = publication_gate.run_record.toplevel(Path.cwd())
    if top is None:
        sys.stderr.write("commit-gate: not in a git repository\n")
        return 2
    # GIT_INDEX_FILE stays as git set it: `git commit -a` and `git commit <path>` stage into a
    # temporary index, and that index is the one the commit records.
    return run_gate(top)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
