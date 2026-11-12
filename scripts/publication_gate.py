#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Refuse a public push unless every commit after BASE is fit for publication.

    make publication-gate [BASE=<rev>]    (uv run python scripts/publication_gate.py [--base <rev>])

BASE defaults to DEFAULT_BASE, `public/main`: the public main the push builds on, as the last
`git fetch public` left it.

The repository is public, so whatever reaches the remote stays readable even after a later fix.
The gate runs on the checkout before every push and refuses with a named code:

    preconditions (stop the gate)
      DIRTY_TREE             uncommitted or untracked changes: the scans would not see them
      BASE_NOT_ANCESTOR      BASE does not name a commit that HEAD descends from
      TOOL_VERSION_MISMATCH  gitleaks is absent or not the CONTRIBUTING.md Maintainer tools pin
    checks (every one runs, whatever an earlier one found)
      SCANNER_NOT_PROVEN     gitleaks missed a runtime-generated access key: its pass proves nothing
      SECRET_IN_PATCH        gitleaks git over BASE..HEAD found a secret in a change
      SECRET_IN_MESSAGE      a commit message or identity line holds a secret (gitleaks git does
                             not read messages, so each one is scanned as a file)
      FORBIDDEN_PATH         a commit adds or modifies local-only material (.agents/, internal/,
                             security-findings*, .env*, local steering, .runs/)
      TRACKED_IGNORED        HEAD tracks a path the ignore rules exclude
      DENYLIST_MISSING       the gitignored local-denylist.txt is absent: without it nothing is
                             checked, and a push needs no owner go once this gate passes
      DENYLIST_EMPTY         local-denylist.txt holds no entry (only blank and # lines)
      DENYLIST_HIT           a literal from local-denylist.txt appears in a message, an added
                             line or the HEAD content of a changed file; the output names the
                             denylist line, never the literal
      IDENTITY_NOT_ALLOWED   an author or committer email outside publication.allowedEmail
                             (git config; falls back to user.email; refuses when both are unset)
      AUDIT_FINDING          npm audit (HIGH and above) or pip-audit finds, in a changed
                             lockfile, an advisory whose ID and aliases are all new versus
                             BASE's lockfile, or cannot run on either side; a pre-existing
                             advisory prints a WARN line naming its register row instead.
                             pip-audit skips only the reviewed IDs in PIP_AUDIT_REVIEWED
      RUN_RECORD_MISSING     no passing, canonical, clean, verified run record of pytest-unit,
                             frontend-test and infra-test for HEAD's tree (docs/test-run-records.md)
      EXPORT_GATE_FAILED     scripts/check-publication.sh (the export stage) failed

Each check prints `publication-gate: <check> ok` or one `REFUSED <CODE> <detail>` line per finding,
then one `WARN <detail>` line per warning, and the gate writes a `publication-gate` run record of
every result. A warning never changes the exit code. Exit 0 only when nothing is
refused. There is no flag that skips a check. Touches no AWS.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

SCRIPTS = Path(__file__).resolve().parent
CONTRIBUTING = SCRIPTS.parent / "CONTRIBUTING.md"
KIND = "publication-gate"
REQUIRED_RECORD_KINDS = ("pytest-unit", "frontend-test", "infra-test")
NPM_LOCKFILES = (
    "frontend/package-lock.json",
    "infra/package-lock.json",
    "scripts/package-lock.json",
)
DENYLIST = "local-denylist.txt"
# The closed set of pip-audit advisories the owner reviewed, each with the docs/security-
# dispositions.md row that owns it. pip-audit matches the ID and its aliases; every other finding
# is compared with BASE (check_audit). Add an entry only with an owner-approved row; remove it
# when its trigger fires.
PIP_AUDIT_REVIEWED = (
    # O-OTEL-TRACESTATE-001 (CVE-2026-114603): NOT AFFECTED, owner 2026-10-23.
    ("GHSA-xcq7-p3pf-p88h", "O-OTEL-TRACESTATE-001"),
)
# The public remote's main, which every push builds on (owner decision, 2026-10-20).
DEFAULT_BASE = "public/main"
ALLOWED_EMAIL_KEY = "publication.allowedEmail"
# gitleaks exits 1 on an error as well as on a leak; a distinct leak code keeps them apart.
LEAK_EXIT = 99
# The RFC 4648 base32 alphabet, derived rather than spelled out.
BASE32 = string.ascii_uppercase + "234567"
GITLEAKS_ENV = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")
# npm audit --audit-level=high refuses at these severities; lower ones are not compared.
NPM_REFUSED_SEVERITIES = frozenset({"high", "critical"})
# Where a pre-existing advisory's register row is looked up, in order: the local register beside
# the checkout, the local register of the main checkout when the gate runs in a worktree, then the
# tracked dispositions.
LOCAL_REGISTER = "security-findings.md"
TRACKED_REGISTER = Path("docs") / "security-dispositions.md"
GHSA = re.compile(r"GHSA(?:-[0-9a-z]{4}){3}")
WITHHELD = "[withheld]"
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


run_record = _sibling("run_record")
tool_versions = _sibling("tool_versions")


class GateError(Exception):
    """A command a check depends on failed; the check refuses rather than passing unexamined."""


@dataclass(frozen=True)
class Refusal:
    """One finding: the refusal code and a detail that never holds a secret or denylisted text."""

    code: str
    detail: str


@dataclass
class Context:
    """What every check reads: the repository, the range and a private scratch directory."""

    root: Path
    base: str
    head: str
    tree: str
    runs_root: Path
    work: Path
    denylist: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    attachments: dict[str, bytes] = field(default_factory=dict[str, bytes])
    # What a check reports without refusing; evaluate moves each check's entries to its Result.
    warnings: list[str] = field(default_factory=list[str])

    @property
    def gitleaks_config(self) -> Path | None:
        """Return the repository's gitleaks config, which extends the default rules."""
        config = self.root / ".gitleaks.toml"
        return config if config.is_file() else None

    def commits(self) -> list[str]:
        """Return every commit after BASE up to HEAD, oldest first."""
        return git_text(self.root, "rev-list", "--reverse", f"{self.base}..{self.head}").split()


@dataclass(frozen=True)
class Check:
    """A named check and the code it refuses with when it cannot run at all."""

    name: str
    code: str
    run: Callable[[Context], list[Refusal]]


@dataclass(frozen=True)
class Result:
    """What one check found, what it warned about, and how long it took."""

    name: str
    code: str
    refusals: list[Refusal]
    duration_s: float
    warnings: list[str] = field(default_factory=list[str])


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def _run(  # noqa: PLR0913 - stdin and env are keyword-only options
    argv: Sequence[str], cwd: Path, *, stdin: bytes | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        # A-SUBPROCESS-001: fixed tool argv built in this module; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        return subprocess.run(  # noqa: S603
            list(argv), cwd=cwd, input=stdin, capture_output=True, check=False, env=env
        )
    except OSError as error:
        raise GateError(f"{argv[0]} could not start: {error}") from error


def _last_line(data: bytes) -> str:
    lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


def git_bytes(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Return git's stdout, raising GateError when it fails."""
    result = _run(["git", *args], root, stdin=stdin)
    if result.returncode != 0:
        raise GateError(f"git {args[0]} failed: {_last_line(result.stderr)}")
    return result.stdout


def git_text(root: Path, *args: str) -> str:
    """Return git's stripped stdout as text, raising GateError when it fails."""
    return git_bytes(root, *args).decode("utf-8", "replace").strip()


def _nul_paths(data: bytes) -> list[str]:
    return [p for p in data.decode("utf-8", "surrogateescape").split("\0") if p]


def gitleaks(ctx: Context, args: Sequence[str], name: str) -> list[dict[str, Any]]:
    """Run gitleaks with the repository config and return its findings (redacted)."""
    report = ctx.work / f"gitleaks-{name}.json"
    argv = [
        "gitleaks",
        *args,
        "--redact",
        "--no-banner",
        "--exit-code",
        str(LEAK_EXIT),
        "--report-format",
        "json",
        "--report-path",
        str(report),
    ]
    config = ctx.gitleaks_config
    if config is not None:
        argv += ["-c", str(config)]
    env = {k: v for k, v in os.environ.items() if k not in GITLEAKS_ENV}
    # The scratch directory as cwd: no stray .gitleaksignore there can hide a finding.
    result = _run(argv, ctx.work, env=env)
    if result.returncode not in {0, LEAK_EXIT}:
        raise GateError(f"gitleaks exited {result.returncode}: {_last_line(result.stderr)}")
    try:
        findings: object = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError(f"gitleaks wrote no readable report: {error}") from error
    if not isinstance(findings, list):
        raise GateError("gitleaks report is not a list")
    found = [
        cast("dict[str, Any]", f) for f in cast("list[object]", findings) if isinstance(f, dict)
    ]
    if result.returncode == LEAK_EXIT and not found:
        raise GateError("gitleaks reported leaks but listed none")
    if result.returncode == 0 and found:
        raise GateError("gitleaks exited 0 but listed findings")
    return found


def _finding(finding: dict[str, Any], where: str) -> str:
    rule = finding.get("RuleID", "unknown-rule")
    line = finding.get("StartLine", "?")
    return f"{rule} at {where}:{line}"


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------


def check_scanner_control(ctx: Context) -> list[Refusal]:
    """Gitleaks must find a runtime-generated access key, or its later passes prove nothing."""
    control = ctx.work / "control"
    control.mkdir()
    # Key IDs are base32, which the aws-access-token rule requires; 16 distinct characters keep
    # the token above the rule's entropy floor on every run. It is never written in a repository.
    tail = "".join(secrets.SystemRandom().sample(BASE32, 16))
    (control / "credentials.txt").write_text(f"aws_access_key_id = AKIA{tail}\n")
    findings = gitleaks(ctx, ["dir", str(control)], "control")
    if any(f.get("RuleID") == "aws-access-token" for f in findings):
        return []
    return [Refusal("SCANNER_NOT_PROVEN", "gitleaks did not find a planted access key")]


def check_secret_in_patch(ctx: Context) -> list[Refusal]:
    """Scan every change after BASE with gitleaks git."""
    findings = gitleaks(ctx, ["git", str(ctx.root), f"--log-opts={ctx.base}..{ctx.head}"], "patch")
    return [
        Refusal(
            "SECRET_IN_PATCH",
            _finding(f, f"commit {str(f.get('Commit', '?'))[:12]} {f.get('File', '?')}"),
        )
        for f in findings
    ]


def message_text(ctx: Context, sha: str) -> bytes:
    """Return a commit's full message and its author and committer lines."""
    return git_bytes(ctx.root, "log", "-1", "--format=%B%n%an <%ae>%n%cn <%ce>", sha)


def check_secret_in_message(ctx: Context) -> list[Refusal]:
    """Write each message to a file and scan the files: gitleaks git does not read messages."""
    messages = ctx.work / "messages"
    messages.mkdir()
    for sha in ctx.commits():
        (messages / f"{sha}.txt").write_bytes(message_text(ctx, sha))
    findings = gitleaks(ctx, ["dir", str(messages)], "messages")
    return [
        Refusal(
            "SECRET_IN_MESSAGE",
            _finding(f, f"commit {Path(str(f.get('File', '?'))).stem[:12]} message"),
        )
        for f in findings
    ]


def forbidden_reason(path: str) -> str | None:
    """Return why `path` must never be published, or None when it may be.

    Matched case-insensitively: git records a path as typed, so `.Agents/` is the same local
    directory on a case-insensitive filesystem and is refused like `.agents/`.
    """
    folded = path.casefold()
    parts = folded.split("/")
    directories, name = parts[:-1], parts[-1]
    for local in (".agents", "internal", ".runs"):
        if local in directories:
            return f"{local}/ is local working material"
    if name.startswith("security-findings"):
        return "security-findings files are local"
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return ".env files hold local configuration"
    if folded.startswith(".kiro/steering/local-"):
        return "local steering is operator-specific"
    return None


def check_forbidden_paths(ctx: Context) -> list[Refusal]:
    """Refuse a commit that adds or modifies a local-only path, even if a later one removes it."""
    refusals: list[Refusal] = []
    for sha in ctx.commits():
        changed = git_bytes(
            ctx.root,
            "diff-tree",
            "-r",
            "-z",
            "-m",
            "--root",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=ACMRT",
            sha,
        )
        for path in sorted(set(_nul_paths(changed))):
            reason = forbidden_reason(path)
            if reason is not None:
                refusals.append(Refusal("FORBIDDEN_PATH", f"commit {sha[:12]} {path}: {reason}"))
    return refusals


def check_tracked_ignored(ctx: Context) -> list[Refusal]:
    """Refuse a HEAD path that the ignore rules exclude: it was force-added or slipped past them."""
    tracked = git_bytes(ctx.root, "ls-tree", "-r", "-z", "--name-only", ctx.head)
    result = _run(["git", "check-ignore", "-z", "--no-index", "--stdin"], ctx.root, stdin=tracked)
    if result.returncode not in {0, 1}:
        raise GateError(f"git check-ignore failed: {_last_line(result.stderr)}")
    return [
        Refusal("TRACKED_IGNORED", f"{path} is tracked at HEAD but ignored")
        for path in _nul_paths(result.stdout)
    ]


def load_denylist(root: Path) -> list[tuple[int, str]]:
    """Return (line number, case-folded literal) per entry; blank and # lines are skipped."""
    path = root / DENYLIST
    if not path.is_file():
        return []
    entries: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        literal = line.strip()
        if literal and not literal.startswith("#"):
            entries.append((number, literal.casefold()))
    return entries


def _patch_sources(ctx: Context) -> list[tuple[str, str]]:
    """Return (where, text) for every added line and header of every patch after BASE."""
    patch = git_bytes(
        ctx.root,
        "log",
        "-p",
        "-m",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--format=%x01%H",
        f"{ctx.base}..{ctx.head}",
    ).decode("utf-8", "replace")
    sources: list[tuple[str, str]] = []
    sha, path = "?", "?"
    for line in patch.splitlines():
        if line.startswith("\x01"):
            sha = line[1:13]
            continue
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1]
        # Removed and context lines are not new content; headers and added lines are.
        if line.startswith(("-", " ")) or not line:
            continue
        sources.append((f"commit {sha} patch {path}", line))
    return sources


def _head_sources(ctx: Context) -> list[tuple[str, str]]:
    """Return (where, text) per line of the HEAD content of every file changed after BASE."""
    changed = _nul_paths(
        git_bytes(ctx.root, "diff", "--name-only", "-z", "--diff-filter=ACMRT", ctx.base, ctx.head)
    )
    if not changed:
        return []
    requests = "".join(f"{ctx.head}:{path}\n" for path in changed).encode(
        "utf-8", "surrogateescape"
    )
    batch = git_bytes(ctx.root, "cat-file", "--batch", stdin=requests)
    sources: list[tuple[str, str]] = []
    offset = 0
    for path in changed:
        end = batch.index(b"\n", offset)
        header = batch[offset:end].split()
        if len(header) != 3:  # noqa: PLR2004 - "<sha> <type> <size>"; "missing" has two fields
            raise GateError(f"git cat-file could not read HEAD:{path}")
        size = int(header[2])
        content = batch[end + 1 : end + 1 + size].decode("utf-8", "replace")
        offset = end + 1 + size + 1
        for number, line in enumerate(content.splitlines(), start=1):
            sources.append((f"HEAD:{path} line {number}", line))
    return sources


def check_denylist(ctx: Context) -> list[Refusal]:
    """Refuse an absent or empty denylist, and any literal of it in messages or changed content."""
    if not (ctx.root / DENYLIST).is_file():
        return [Refusal("DENYLIST_MISSING", f"{DENYLIST} is absent from the checkout root")]
    if not ctx.denylist:
        return [Refusal("DENYLIST_EMPTY", f"{DENYLIST} holds no entry")]
    sources = [
        (f"commit {sha[:12]} message", message_text(ctx, sha).decode("utf-8", "replace"))
        for sha in ctx.commits()
    ]
    sources += _patch_sources(ctx)
    sources += _head_sources(ctx)
    refusals: list[Refusal] = []
    seen: set[tuple[int, str]] = set()
    for where, text in sources:
        folded = text.casefold()
        for number, literal in ctx.denylist:
            if literal in folded and (number, where) not in seen:
                seen.add((number, where))
                refusals.append(Refusal("DENYLIST_HIT", f"{DENYLIST} line {number} in {where}"))
    return refusals


def allowed_emails(root: Path) -> list[str]:
    """Return publication.allowedEmail values, else user.email, case-folded; [] when unset."""
    for key, many in ((ALLOWED_EMAIL_KEY, True), ("user.email", False)):
        argv = ["git", "config", "--get-all" if many else "--get", key]
        result = _run(argv, root)
        if result.returncode == 0:
            values = result.stdout.decode("utf-8", "replace").split()
            if values:
                return [v.casefold() for v in values]
        elif result.returncode != 1:
            raise GateError(f"git config {key} failed: {_last_line(result.stderr)}")
    return []


def check_identity(ctx: Context) -> list[Refusal]:
    """Refuse an author or committer email outside the operator's allowlist."""
    allowed = allowed_emails(ctx.root)
    if not allowed:
        return [
            Refusal(
                "IDENTITY_NOT_ALLOWED",
                f"neither {ALLOWED_EMAIL_KEY} nor user.email is set in git config",
            )
        ]
    log = git_text(ctx.root, "log", "--format=%H%x1f%ae%x1f%ce", f"{ctx.base}..{ctx.head}")
    refusals: list[Refusal] = []
    for line in log.splitlines():
        sha, author, committer = line.split("\x1f")
        for role, email in (("author", author), ("committer", committer)):
            if email.casefold() not in allowed:
                refusals.append(
                    Refusal("IDENTITY_NOT_ALLOWED", f"commit {sha[:12]} {role} {email}")
                )
    return refusals


@dataclass(frozen=True)
class Advisory:
    """One advisory a scanner reported: its ID, its aliases and the package it names."""

    id: str
    aliases: tuple[str, ...]
    package: str

    @property
    def ids(self) -> frozenset[str]:
        """Return the ID and every alias; two advisories are the same when these intersect."""
        return frozenset({self.id, *self.aliases})

    @property
    def label(self) -> str:
        """Return the ID with its aliases, for a refusal or warning line."""
        return " / ".join([self.id, *sorted(set(self.aliases) - {self.id})])


def _json_object(data: bytes, what: str) -> dict[str, Any]:
    try:
        parsed: object = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise GateError(f"{what} wrote no readable JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise GateError(f"{what} JSON is not an object")
    return cast("dict[str, Any]", parsed)


def _base_files(ctx: Context, paths: Sequence[str], directory: Path) -> bool:
    """Write BASE's copy of each path that exists there into `directory`; False when none does."""
    listed = set(_nul_paths(git_bytes(ctx.root, "ls-tree", "-z", "--name-only", ctx.base, *paths)))
    directory.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path in listed:
            content = git_bytes(ctx.root, "show", f"{ctx.base}:{path}")
            (directory / Path(path).name).write_bytes(content)
    return bool(listed)


def npm_advisories(directory: Path, what: str) -> list[Advisory]:
    """Return every HIGH or CRITICAL advisory npm audit reports for the lockfile in `directory`."""
    result = _run(
        ["npm", "audit", "--audit-level=high", "--package-lock-only", "--json"], directory
    )
    if result.returncode not in {0, 1}:
        raise GateError(f"{what} exited {result.returncode}: {_last_line(result.stderr)}")
    report = _json_object(result.stdout, what)
    if "error" in report:
        raise GateError(f"{what} reported an error: {str(report['error'])[:200]}")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise GateError(f"{what} JSON has no vulnerabilities object")
    found: dict[tuple[str, str], Advisory] = {}
    for entry in cast("dict[str, object]", vulnerabilities).values():
        if not isinstance(entry, dict):
            continue
        for via in cast("list[object]", cast("dict[str, Any]", entry).get("via", [])):
            # A string `via` names the dependency that carries the advisory; only objects are one.
            if not isinstance(via, dict):
                continue
            item = cast("dict[str, Any]", via)
            if str(item.get("severity")) not in NPM_REFUSED_SEVERITIES:
                continue
            ghsa = GHSA.search(str(item.get("url", "")))
            source = str(item.get("source", "?"))
            primary = ghsa.group(0) if ghsa else f"npm-advisory-{source}"
            package = str(item.get("name", "?"))
            found[(primary, package)] = Advisory(primary, (f"npm-advisory-{source}",), package)
    if result.returncode == 1 and not found:
        raise GateError(f"{what} exited 1 but listed no HIGH or CRITICAL advisory")
    return list(found.values())


def pip_advisories(ctx: Context, project: Path | None, what: str) -> list[Advisory]:
    """Return every advisory pip-audit reports for the uv.lock of `project` (HEAD when None)."""
    side = "head" if project is None else "base"
    requirements = ctx.work / f"requirements-{side}.txt"
    report = ctx.work / f"pip-audit-{side}.json"
    directory = [] if project is None else ["--directory", str(project)]
    export = _run(
        [
            "uv",
            "export",
            *directory,
            "--frozen",
            "--all-extras",
            "--all-groups",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "-o",
            str(requirements),
        ],
        ctx.root,
    )
    if export.returncode != 0:
        raise GateError(f"uv export exited {export.returncode}: {_last_line(export.stderr)}")
    audit = _run(
        [
            "uv",
            "run",
            "--frozen",
            "pip-audit",
            "--strict",
            "--disable-pip",
            *(arg for vuln, _ in PIP_AUDIT_REVIEWED for arg in ("--ignore-vuln", vuln)),
            "-r",
            str(requirements),
            "--format",
            "json",
            "--output",
            str(report),
        ],
        ctx.root,
    )
    if audit.returncode not in {0, 1}:
        raise GateError(
            f"{what} exited {audit.returncode}: {_last_line(audit.stdout + audit.stderr)}"
        )
    try:
        data = report.read_bytes()
    except OSError as error:
        raise GateError(f"{what} wrote no report: {error}") from error
    dependencies = _json_object(data, what).get("dependencies")
    if not isinstance(dependencies, list):
        raise GateError(f"{what} JSON has no dependencies list")
    found = [
        Advisory(str(vuln["id"]), tuple(str(a) for a in vuln.get("aliases", [])), str(dep["name"]))
        for dep in cast("list[dict[str, Any]]", dependencies)
        for vuln in cast("list[dict[str, Any]]", dep.get("vulns", []))
    ]
    if audit.returncode == 1 and not found:
        raise GateError(
            f"{what} exited 1 but listed no advisory: {_last_line(audit.stdout + audit.stderr)}"
        )
    return found


def register_files(root: Path) -> list[Path]:
    """Return the registers a pre-existing advisory's row is looked up in, in order."""
    common = Path(git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    candidates = [root / LOCAL_REGISTER, common.parent / LOCAL_REGISTER, root / TRACKED_REGISTER]
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def register_row(registers: Sequence[Path], advisory: Advisory) -> str | None:
    """Return the ID of the first register row that names any of the advisory's identifiers."""
    # A bare numeric npm source would match unrelated text; only named identifiers are looked up.
    names = [i for i in advisory.ids if not i.startswith("npm-advisory-")]
    patterns = [re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])") for name in names]
    for register in registers:
        try:
            text = register.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            cells = line.split("|")
            if not line.startswith("| ") or len(cells) < 3:  # noqa: PLR2004 - "| id | ..."
                continue
            if any(p.search(line) for p in patterns):
                return cells[1].strip()
    return None


def compare_advisories(
    ctx: Context, what: str, sides: tuple[Sequence[Advisory], Sequence[Advisory]]
) -> list[Refusal]:
    """Refuse each HEAD advisory none of whose identifiers BASE reports; warn on the rest.

    `sides` is (HEAD's advisories, BASE's). Each warning goes to `ctx.warnings`.
    """
    head, base = sides
    known = frozenset(i for advisory in base for i in advisory.ids)
    refusals: list[Refusal] = []
    registers: list[Path] | None = None
    for advisory in head:
        if not advisory.ids & known:
            detail = f"{what} {advisory.label} in {advisory.package} is new versus BASE"
            refusals.append(Refusal("AUDIT_FINDING", detail))
            continue
        registers = register_files(ctx.root) if registers is None else registers
        row = register_row(registers, advisory)
        where = f"register row {row}" if row else "no register row"
        ctx.warnings.append(
            f"{what} {advisory.label} in {advisory.package} is pre-existing versus BASE ({where})"
        )
    return refusals


def _npm_sides(ctx: Context, lockfile: str) -> tuple[list[Advisory], list[Advisory]]:
    parent = Path(lockfile).parent
    what = f"npm audit {lockfile}"
    head = npm_advisories(ctx.root / parent, what)
    base_dir = ctx.work / "base" / parent
    _base_files(ctx, [f"{parent}/package.json", f"{parent}/.npmrc", lockfile], base_dir)
    has_lock = (base_dir / "package-lock.json").is_file()
    return head, npm_advisories(base_dir, f"{what} (BASE)") if has_lock else []


def _uv_sides(ctx: Context) -> tuple[list[Advisory], list[Advisory]]:
    what = "pip-audit uv.lock"
    head = pip_advisories(ctx, None, what)
    base_dir = ctx.work / "base" / "python"
    _base_files(ctx, ["pyproject.toml", "uv.lock"], base_dir)
    has_lock = (base_dir / "uv.lock").is_file() and (base_dir / "pyproject.toml").is_file()
    return head, pip_advisories(ctx, base_dir, f"{what} (BASE)") if has_lock else []


def check_audit(ctx: Context) -> list[Refusal]:
    """Audit every changed lockfile against BASE's: refuse only advisories new in HEAD.

    An advisory is its ID plus aliases; it is pre-existing when any of them is among BASE's. A
    pre-existing one warns with the register row that owns it ("no register row" when none does)
    and never refuses. A scanner that cannot run, on either side, refuses.
    """
    changed = set(_nul_paths(git_bytes(ctx.root, "diff", "--name-only", "-z", ctx.base, ctx.head)))
    audited = [lock for lock in NPM_LOCKFILES if lock in changed]
    if "uv.lock" in changed:
        audited.append("uv.lock")
    if not audited:
        return []
    refusals: list[Refusal] = []
    for lockfile in audited:
        what = "pip-audit uv.lock" if lockfile == "uv.lock" else f"npm audit {lockfile}"
        try:
            sides = _uv_sides(ctx) if lockfile == "uv.lock" else _npm_sides(ctx, lockfile)
        except GateError as error:
            refusals.append(Refusal("AUDIT_FINDING", f"{what} could not run: {error}"))
            continue
        refusals += compare_advisories(ctx, what, sides)
    return refusals


def check_run_records(ctx: Context) -> list[Refusal]:
    """Require a passing, canonical, clean, verified record of each test kind for HEAD's tree."""
    return [
        Refusal(
            "RUN_RECORD_MISSING",
            f"no passing canonical clean {kind} record for tree {ctx.tree[:12]} "
            f"under {ctx.runs_root}",
        )
        for kind in REQUIRED_RECORD_KINDS
        if not run_record.find_records(
            ctx.runs_root, kind, tree=ctx.tree, passing=True, canonical=True, clean=True
        )
    ]


def check_export(ctx: Context) -> list[Refusal]:
    """Run the export stage (file classes, boundary, links, headers, export scan)."""
    result = _run(["bash", str(SCRIPTS / "check-publication.sh"), ctx.base], ctx.root)
    ctx.attachments["export.log"] = result.stdout + result.stderr
    if result.returncode == 0:
        return []
    detail = f"check-publication.sh exited {result.returncode}: {_last_line(result.stderr)}"
    return [Refusal("EXPORT_GATE_FAILED", detail)]


DEFAULT_CHECKS: tuple[Check, ...] = (
    Check("scanner-control", "SCANNER_NOT_PROVEN", check_scanner_control),
    Check("secret-in-patch", "SECRET_IN_PATCH", check_secret_in_patch),
    Check("secret-in-message", "SECRET_IN_MESSAGE", check_secret_in_message),
    Check("forbidden-path", "FORBIDDEN_PATH", check_forbidden_paths),
    Check("tracked-ignored", "TRACKED_IGNORED", check_tracked_ignored),
    Check("denylist", "DENYLIST_HIT", check_denylist),
    Check("identity", "IDENTITY_NOT_ALLOWED", check_identity),
    Check("audit", "AUDIT_FINDING", check_audit),
    Check("run-records", "RUN_RECORD_MISSING", check_run_records),
    Check("export", "EXPORT_GATE_FAILED", check_export),
)


def evaluate(
    ctx: Context, checks: Sequence[Check], on_start: Callable[[str], None] | None = None
) -> list[Result]:
    """Run every check, whatever an earlier one found; a check that cannot run refuses."""
    results: list[Result] = []
    for check in checks:
        if on_start is not None:
            on_start(check.name)
        start = time.monotonic()
        mark = len(ctx.warnings)
        try:
            refusals = check.run(ctx)
        except (GateError, OSError, ValueError, UnicodeError) as error:
            refusals = [Refusal(check.code, f"the check could not run: {error}")]
        warnings = ctx.warnings[mark:]
        results.append(
            Result(check.name, check.code, refusals, round(time.monotonic() - start, 6), warnings)
        )
    return results


# --------------------------------------------------------------------------------------------
# Preconditions, output and the record
# --------------------------------------------------------------------------------------------


def preconditions(root: Path, base_arg: str, contributing: Path) -> tuple[str | None, list[Result]]:
    """Return the resolved BASE (or None) and the precondition results."""
    results: list[Result] = []
    status = _run(["git", "status", "--porcelain", "--untracked-files=all"], root)
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    clean: list[Refusal] = []
    if status.returncode != 0:
        clean = [Refusal("DIRTY_TREE", f"git status failed: {_last_line(status.stderr)}")]
    elif dirty:
        clean = [Refusal("DIRTY_TREE", f"{len(dirty)} uncommitted or untracked path(s)")]
    results.append(Result("clean-tree", "DIRTY_TREE", clean, 0.0))

    base: str | None = None
    resolved = _run(["git", "rev-parse", "--verify", "-q", f"{base_arg}^{{commit}}"], root)
    ancestry: list[Refusal] = []
    if resolved.returncode != 0:
        ancestry = [Refusal("BASE_NOT_ANCESTOR", f"BASE {base_arg} does not name a commit")]
    else:
        base = resolved.stdout.decode().strip()
        is_ancestor = _run(["git", "merge-base", "--is-ancestor", base, "HEAD"], root)
        if is_ancestor.returncode != 0:
            ancestry = [
                Refusal("BASE_NOT_ANCESTOR", f"BASE {base[:12]} is not an ancestor of HEAD")
            ]
    results.append(Result("base", "BASE_NOT_ANCESTOR", ancestry, 0.0))

    try:
        problem = tool_versions.mismatch(
            "gitleaks", tool_versions.installed_gitleaks(), contributing
        )
    except tool_versions.ToolVersionError as error:
        problem = str(error)
    version = [Refusal("TOOL_VERSION_MISMATCH", problem)] if problem else []
    results.append(Result("tool-version", "TOOL_VERSION_MISMATCH", version, 0.0))
    return base, results


def redact(text: str, denylist: Sequence[tuple[int, str]]) -> str:
    """Replace every denylisted literal, in any letter case, so output never carries one."""
    for _, literal in denylist:
        text = re.sub(re.escape(literal), WITHHELD, text, flags=re.IGNORECASE)
    return text


def _lines(stage: str, result: Result, denylist: Sequence[tuple[int, str]]) -> list[str]:
    label = f"{stage}{result.name}"
    lines = [
        f"publication-gate: {label} REFUSED {r.code} {redact(r.detail, denylist)}"
        for r in result.refusals
    ] or [f"publication-gate: {label} ok"]
    return lines + [
        f"publication-gate: {label} WARN {redact(warning, denylist)}" for warning in result.warnings
    ]


def _utc(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _steps(results: Sequence[Result], denylist: Sequence[tuple[int, str]]) -> list[dict[str, Any]]:
    return [
        {
            "name": r.name,
            "code": r.code,
            "exit_code": EXIT_REFUSED if r.refusals else 0,
            "duration_s": r.duration_s,
            "refusals": [
                {"code": f.code, "detail": redact(f.detail, denylist)} for f in r.refusals
            ],
            "warnings": [redact(w, denylist) for w in r.warnings],
            "child_records": [],
        }
        for r in results
    ]


def run_gate(  # noqa: PLR0913 - the CLI's inputs plus the injection points the tests use
    root: Path,
    base_arg: str,
    checks: Sequence[Check] = DEFAULT_CHECKS,
    *,
    runs_root: Path | None = None,
    contributing: Path = CONTRIBUTING,
    out: Callable[[str], None] = print,
) -> int:
    """Run the preconditions and every check, print one line per result, record it as it runs."""
    started, utc_start = time.monotonic(), datetime.now(UTC)
    runs = run_record.record_root(runs_root, cwd=root)
    denylist = load_denylist(root)
    porcelain_before = run_record.porcelain_count(root)
    head = git_text(root, "rev-parse", "HEAD")
    tree = git_text(root, "rev-parse", "HEAD^{tree}")
    record: dict[str, Any] = {
        "canonical": tuple(checks) == DEFAULT_CHECKS,
        "argv": ["publication_gate.py", "--base", base_arg],
        "cwd": ".",
        "head": head,
        "tree": tree,
        "branch": git_text(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "base": None,
        "source_commit": head,
        "context": "checkout",
        "utc_start": _utc(utc_start),
        "porcelain_before": porcelain_before,
    }
    done: list[Result] = []
    try:
        live = run_record.LiveRecord.begin(
            KIND,
            record,
            root=runs,
            now=utc_start,
            progress=lambda: {"checks_done": len(done), "checks_total": len(checks)},
        )
    except (run_record.RunRecordError, OSError) as error:
        out(f"publication-gate: REFUSED RECORD_NOT_WRITTEN {error}")
        return EXIT_REFUSED

    def say(line: str) -> None:
        out(line)
        live.line(line)

    def emit(stage: str, result: Result) -> None:
        for line in _lines(stage, result, denylist):
            say(line)

    live.set_step("preconditions")
    base, results = preconditions(root, base_arg, contributing)
    for result in results:
        emit("precondition ", result)
    attachments: dict[str, bytes] = {}
    if base is not None and not any(r.refusals for r in results):
        work = Path(tempfile.mkdtemp(prefix="publication-gate-"))
        try:
            ctx = Context(root, base, head, tree, runs, work, denylist)
            for result in evaluate(ctx, checks, live.set_step):
                emit("", result)
                results.append(result)
                done.append(result)
            for name, data in ctx.attachments.items():
                attachments[name] = redact(data.decode("utf-8", "replace"), denylist).encode()
        finally:
            shutil.rmtree(work, ignore_errors=True)
    live.set_step(None)

    refused = sorted({f.code for r in results for f in r.refusals})
    exit_code = EXIT_REFUSED if refused else 0
    porcelain_after = run_record.porcelain_count(root)
    verdict = f"REFUSED {' '.join(refused)}" if refused else "passed"
    summary = f"publication-gate: {verdict} for HEAD {head[:12]}"
    record.update(
        {
            "base": base,
            "utc_end": _utc(datetime.now(UTC)),
            "duration_s": round(time.monotonic() - started, 6),
            "porcelain_after": porcelain_after,
            "clean": porcelain_before == 0 and porcelain_after == 0,
            "exit_code": exit_code,
            "outcome": "fail" if refused else "pass",
            "report_missing": False,
            "refused": refused,
            # A gate kind's aggregate; no publication check writes a record of its own.
            "child_records": [],
            "tool_versions": {
                **run_record.tool_versions(root, None),
                "gitleaks": tool_versions.installed_gitleaks(),
            },
        }
    )
    live.line(summary)
    try:
        directory = live.finalise(record, steps=_steps(results, denylist), files=attachments)
    except (run_record.RunRecordError, OSError) as error:
        out(f"publication-gate: REFUSED RECORD_NOT_WRITTEN {error}")
        return EXIT_REFUSED
    out(summary)
    out(f"run-record: {directory}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Parse --base and run the full gate on the repository containing the working directory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"the public main the push builds on (default {DEFAULT_BASE})",
    )
    args = parser.parse_args(argv)
    top = run_record.toplevel(Path.cwd())
    if top is None:
        sys.stderr.write("publication-gate: not in a git repository\n")
        return 2
    try:
        return run_gate(top, args.base)
    except GateError as error:
        sys.stderr.write(f"publication-gate: REFUSED {error}\n")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
