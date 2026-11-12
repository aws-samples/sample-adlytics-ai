# SPDX-License-Identifier: MIT-0
"""The publication gate refuses every commit range that would leak, with the exact refusal code.

`scripts/publication_gate.py` runs before every push to the public remote; whatever passes it is
readable by anyone, permanently. A failure here means a secret or internal identifier reaching the
public remote: a key in a commit message or a file, a denylisted internal name, local working
material, a force-added ignored file, an unexpected identity, or a push whose tests were never run
on that exact tree.

Each test builds a throwaway repository and commits with plumbing (`git write-tree`, `git
commit-tree`, `git update-ref`), so no commit hook runs and none needs bypassing. Key-shaped tokens
are generated at runtime and never written in a tracked file. Global and system git configuration
are replaced with an empty file, so the operator's identity and ignore rules never decide a test.
The export stage (scripts/check-publication.sh) needs this repository's Makefile and is excluded
here; tests/unit/test_check_publication.py covers it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from gate_script_support import SCRIPTS, git

pytestmark = pytest.mark.unit

EMAIL = "fixture@example.invalid"
OUTSIDER = "outsider@example.invalid"
# The RFC 4648 base32 alphabet, derived here independently of the gate under test.
BASE32 = string.ascii_uppercase + "234567"
DENYLISTED = "AcmeInternalTool"
# Every fixture checkout has a denylist, as the push tier requires; no test content matches it.
DEFAULT_DENYLIST = "# fixture entries\nNeverCommittedFixtureName\n"
GITIGNORE = ".runs/\nlocal-denylist.txt\nbuild/\n"
TEST_KINDS = ("pytest-unit", "frontend-test", "infra-test")


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
def gate() -> ModuleType:
    """Load the gate script as a module; it loads the recorder and tool pins beside it."""
    return _load("publication_gate")


@pytest.fixture(scope="module")
def recorder(gate: ModuleType) -> ModuleType:
    """Return the run recorder the gate itself loaded."""
    return gate.run_record


def _pinned_gitleaks_installed() -> bool:
    if shutil.which("gitleaks") is None:
        return False
    versions = _load("tool_versions")
    return versions.mismatch("gitleaks", versions.installed_gitleaks()) is None


needs_gitleaks = pytest.mark.skipif(
    not _pinned_gitleaks_installed(),
    reason="needs the gitleaks version the CONTRIBUTING.md Maintainer tools table pins, on PATH",
)


def _token() -> str:
    """Return an access-key-shaped token: base32, 16 distinct characters after the prefix."""
    return "AKIA" + "".join(secrets.SystemRandom().sample(BASE32, 16))


def _make_writable(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        Path(current).chmod(0o755)
        for name in dirs:
            (Path(current) / name).chmod(0o755)
        for name in files:
            (Path(current) / name).chmod(0o644)


class Repo:
    """A throwaway repository on `main` whose commits are written with plumbing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "publication.allowedEmail", EMAIL)
        self.base = self.commit({".gitignore": GITIGNORE}, message="fixture base", root=True)
        (path / "local-denylist.txt").write_text(DEFAULT_DENYLIST)

    def commit(  # noqa: PLR0913 - keyword-only fixture options
        self,
        files: dict[str, str],
        *,
        message: str = "fixture change",
        email: str = EMAIL,
        force: bool = False,
        root: bool = False,
    ) -> str:
        """Write and stage `files`, commit the index without hooks, and advance main."""
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.path, "add", *(["-f"] if force else []), *files)
        tree = git(self.path, "write-tree")
        parents = [] if root else ["-p", git(self.path, "rev-parse", "HEAD")]
        sha = git(
            self.path,
            "-c",
            "user.name=fixture",
            "-c",
            f"user.email={email}",
            "commit-tree",
            tree,
            *parents,
            "-m",
            message,
        )
        git(self.path, "update-ref", "refs/heads/main", sha)
        return sha

    @property
    def tree(self) -> str:
        """HEAD's tree, which run records are matched against."""
        return git(self.path, "rev-parse", "HEAD^{tree}")

    @property
    def runs(self) -> Path:
        """The gitignored record root the gate reads by default."""
        return self.path / ".runs"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Repo]:
    """Yield an isolated fixture repository; read-only records are made writable for cleanup."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("RUN_RECORD_ROOT", raising=False)
    path = tmp_path / "repo"
    path.mkdir()
    fixture = Repo(path)
    yield fixture
    _make_writable(path / ".runs")


def _write_records(recorder: ModuleType, repo: Repo, *, canonical: bool = True) -> None:
    """Write one passing, clean record of every required test kind for HEAD's tree."""
    for kind in TEST_KINDS:
        run = {
            "canonical": canonical,
            "clean": True,
            "tree": repo.tree,
            "exit_code": 0,
            "report_missing": False,
        }
        test: dict[str, Any] = {
            "node_id": f"{kind}::passes",
            "file": "tests/unit/test_fixture.py",
            "summary": "passes",
            "decision_ids": [],
            "outcome": "passed",
            "duration_s": 0.01,
            "retries": 0,
        }
        recorder.write_record(kind, run, tests=[test], root=repo.runs)


def _run(gate: ModuleType, repo: Repo) -> tuple[int, list[str]]:
    """Run every check except the export stage and return the exit code and the printed lines."""
    checks = tuple(check for check in gate.DEFAULT_CHECKS if check.name != "export")
    lines: list[str] = []
    code = gate.run_gate(repo.path, repo.base, checks, out=lines.append)
    return code, lines


SUMMARY = "publication-gate: REFUSED "


def _findings(lines: list[str], code: str | None = None) -> list[str]:
    """Return the per-check refusal lines (of `code` when given), without the summary line."""
    return [
        line
        for line in lines
        if " REFUSED " in line
        and not line.startswith(SUMMARY)
        and (code is None or f" REFUSED {code} " in line)
    ]


def _refused(lines: list[str]) -> set[str]:
    return {line.split(" REFUSED ", 1)[1].split()[0] for line in _findings(lines)}


def _gate_record(repo: Repo) -> Path:
    records = sorted((repo.runs / "publication-gate").iterdir())
    assert len(records) == 1
    return records[0]


@needs_gitleaks
def test_a_clean_range_with_its_run_records_passes(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    repo.commit({"docs/notes.md": "public notes\n"})
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 0, lines
    assert _refused(lines) == set()
    assert "publication-gate: scanner-control ok" in lines
    record = _gate_record(repo)
    assert recorder.verify_record(record) == []
    run: dict[str, Any] = json.loads((record / "run.json").read_text())
    assert run["outcome"] == "pass"
    assert run["tree"] == repo.tree
    assert run["base"] == repo.base
    # A narrowed check list is never the canonical gate a publish script may accept.
    assert run["canonical"] is False
    # The gate's own tool joins the common set; it never replaces it.
    assert {"python", "uv", "git", "platform", "gitleaks"} <= set(run["tool_versions"])
    # The gate-kind aggregate docs/test-run-records.md defines, empty as every step's is.
    assert run["child_records"] == []
    steps = json.loads((record / "steps.json").read_text())
    assert steps
    assert all(s["child_records"] == [] for s in steps)


@needs_gitleaks
def test_a_key_in_a_commit_message_is_refused(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    token = _token()
    repo.commit({"a.txt": "harmless\n"}, message=f"fix\n\naws_access_key_id = {token}\n")
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"SECRET_IN_MESSAGE"}
    assert token not in "\n".join(lines)


@needs_gitleaks
def test_a_key_in_a_file_is_refused(gate: ModuleType, recorder: ModuleType, repo: Repo) -> None:
    token = _token()
    repo.commit({"config.ini": f"aws_access_key_id = {token}\n"})
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"SECRET_IN_PATCH"}
    assert token not in "\n".join(lines)


@needs_gitleaks
def test_a_denylisted_literal_is_refused_without_being_printed(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    (repo.path / "local-denylist.txt").write_text(f"# internal names\n\n{DENYLISTED}\n")
    repo.commit(
        {"notes.txt": f"built with {DENYLISTED.lower()} today\n"},
        message=f"mention {DENYLISTED.upper()}",
    )
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"DENYLIST_HIT"}
    hits = _findings(lines, "DENYLIST_HIT")
    assert any("message" in line for line in hits)
    assert any("HEAD:notes.txt line 1" in line for line in hits)
    assert all("local-denylist.txt line 3" in line for line in hits)
    record = _gate_record(repo)
    written = "\n".join([*lines, *(p.read_text() for p in record.rglob("*") if p.is_file())])
    assert DENYLISTED.casefold() not in written.casefold()


@needs_gitleaks
def test_a_denylisted_name_in_a_path_alone_is_refused(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    # A tool's output directory needs no tracked ignore rule: listing the tool's name in the
    # denylist refuses a commit that adds it, through the patch headers, on any clone.
    (repo.path / "local-denylist.txt").write_text(f"{DENYLISTED}\n")
    repo.commit({f".{DENYLISTED.lower()}/report.json": "{}\n"}, message="add a report")
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"DENYLIST_HIT"}
    assert any("patch" in line for line in _findings(lines, "DENYLIST_HIT"))
    assert DENYLISTED.casefold() not in "\n".join(lines).casefold()


@needs_gitleaks
def test_local_working_material_is_refused(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    repo.commit({".agents/notes.md": "working notes\n"})
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"FORBIDDEN_PATH"}


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        (".agents/notes.md", True),
        # The rules match a path component or a prefix, never an extension; the custody paths
        # carry none, because make check-publication-boundary refuses a named custody file.
        ("internal/plan", True),
        ("docs/internal/plan.md", True),
        (".runs/x/run.json", True),
        ("security-findings.md", True),
        ("docs/security-findings-2026.md", True),
        (".env", True),
        ("frontend/.env.local", True),
        (".env.example", False),
        (".kiro/steering/local-remotes", True),
        (".kiro/steering/meta-delivery-git.md", False),
        ("docs/internals.md", False),
        # Case variants name the same directory on a case-insensitive filesystem.
        (".Agents/notes.md", True),
        ("docs/Internal/plan.md", True),
        ("Security-Findings.md", True),
        ("frontend/.ENV.local", True),
        (".ENV.EXAMPLE", False),
        (".kiro/steering/Local-remotes", True),
    ],
)
def test_forbidden_paths_are_named(gate: ModuleType, path: str, forbidden: bool) -> None:
    assert (gate.forbidden_reason(path) is not None) is forbidden


@needs_gitleaks
def test_a_force_added_ignored_file_is_refused(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    repo.commit({"build/out.txt": "artefact\n"}, force=True)
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"TRACKED_IGNORED"}
    assert any("build/out.txt" in line for line in lines)


@needs_gitleaks
def test_a_tree_without_run_records_is_refused(gate: ModuleType, repo: Repo) -> None:
    repo.commit({"docs/notes.md": "public notes\n"})

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"RUN_RECORD_MISSING"}
    missing = _findings(lines, "RUN_RECORD_MISSING")
    assert len(missing) == len(TEST_KINDS)


@needs_gitleaks
def test_a_non_canonical_record_does_not_satisfy_the_gate(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    repo.commit({"docs/notes.md": "public notes\n"})
    _write_records(recorder, repo, canonical=False)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"RUN_RECORD_MISSING"}


@pytest.mark.parametrize(
    ("content", "code"),
    [(None, "DENYLIST_MISSING"), ("# only a comment\n\n", "DENYLIST_EMPTY")],
    ids=["absent", "empty"],
)
def test_an_absent_or_empty_denylist_is_refused(  # noqa: PLR0913, PLR0917 - fixtures and a row
    gate: ModuleType, repo: Repo, tmp_path: Path, content: str | None, code: str
) -> None:
    head = repo.commit({"docs/notes.md": "public notes\n"})
    denylist = repo.path / "local-denylist.txt"
    if content is None:
        denylist.unlink()
    else:
        denylist.write_text(content)
    ctx = gate.Context(
        repo.path, repo.base, head, repo.tree, repo.runs, tmp_path, gate.load_denylist(repo.path)
    )

    refusals = gate.check_denylist(ctx)

    assert [(r.code, r.detail.split()[0]) for r in refusals] == [(code, "local-denylist.txt")]


def test_an_unsealed_record_does_not_satisfy_the_gate(  # noqa: PLR0913, PLR0917 - fixtures
    gate: ModuleType, recorder: ModuleType, repo: Repo, tmp_path: Path
) -> None:
    head = repo.commit({"docs/notes.md": "public notes\n"})
    run = {"canonical": True, "clean": True, "tree": repo.tree, "exit_code": 0}
    for kind in TEST_KINDS:
        # Begun and never finalised: what a run in progress, or a killed one, leaves on disk.
        recorder.LiveRecord.begin(kind, run, root=repo.runs, heartbeat=False)
    ctx = gate.Context(repo.path, repo.base, head, repo.tree, repo.runs, tmp_path)

    refusals = gate.check_run_records(ctx)

    assert [r.code for r in refusals] == ["RUN_RECORD_MISSING"] * len(TEST_KINDS)
    stale = time.time() + recorder.STALE_AFTER_S + 1
    states = {recorder.record_state(d, now=stale) for d in recorder.record_dirs(repo.runs)}
    # This process is alive, so its records are stalled, not abandoned; neither is accepted.
    assert states == {"stalled"}


@needs_gitleaks
def test_an_outsider_identity_is_refused(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    repo.commit({"docs/notes.md": "public notes\n"}, email=OUTSIDER)
    _write_records(recorder, repo)

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"IDENTITY_NOT_ALLOWED"}
    assert any(f"author {OUTSIDER}" in line for line in lines)


@needs_gitleaks
def test_an_untracked_file_stops_the_gate_before_any_check(
    gate: ModuleType, recorder: ModuleType, repo: Repo
) -> None:
    repo.commit({"docs/notes.md": "public notes\n"})
    _write_records(recorder, repo)
    (repo.path / "stray.txt").write_text("not committed\n")

    code, lines = _run(gate, repo)

    assert code == 1
    assert _refused(lines) == {"DIRTY_TREE"}
    assert not any("scanner-control" in line for line in lines)


def test_the_full_gate_runs_every_check(gate: ModuleType) -> None:
    codes = [check.code for check in gate.DEFAULT_CHECKS]

    assert codes == [
        "SCANNER_NOT_PROVEN",
        "SECRET_IN_PATCH",
        "SECRET_IN_MESSAGE",
        "FORBIDDEN_PATH",
        "TRACKED_IGNORED",
        "DENYLIST_HIT",
        "IDENTITY_NOT_ALLOWED",
        "AUDIT_FINDING",
        "RUN_RECORD_MISSING",
        "EXPORT_GATE_FAILED",
    ]


REVIEWED_VULN = "GHSA-xcq7-p3pf-p88h"
OLD_VULN = "CVE-2026-0000001"
OLD_ALIAS = "GHSA-aaaa-bbbb-cccc"
NEW_VULN = "CVE-2026-0000002"
NPM_OLD = "GHSA-1111-2222-3333"
NPM_NEW = "GHSA-4444-5555-6666"


def _pip_report(*vulns: tuple[str, tuple[str, ...]]) -> dict[str, Any]:
    """Return pip-audit's JSON shape: one dependency carrying each (id, aliases)."""
    return {
        "dependencies": [
            {
                "name": "fixture-package",
                "version": "1.0",
                "vulns": [
                    {"id": vuln, "aliases": list(aliases), "fix_versions": []}
                    for vuln, aliases in vulns
                ],
            }
        ],
        "fixes": [],
    }


def _npm_report(*vias: tuple[str, int, str]) -> dict[str, Any]:
    """Return npm audit's JSON shape: one package whose `via` has each (GHSA, source, severity)."""
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "fixture-npm": {
                "name": "fixture-npm",
                "severity": "high",
                "via": [
                    "fixture-parent",
                    *(
                        {
                            "source": source,
                            "name": "fixture-npm",
                            "severity": severity,
                            "url": f"https://github.com/advisories/{ghsa}",
                        }
                        for ghsa, source, severity in vias
                    ),
                ],
            }
        },
    }


def _finds(report: object) -> bool:
    """Return whether a stand-in report holds a finding the real tool exits 1 for."""
    if not isinstance(report, dict):
        return False
    data = cast("dict[str, Any]", report)
    if "dependencies" in data:
        return any(dep["vulns"] for dep in cast("list[dict[str, Any]]", data["dependencies"]))
    entries = cast("dict[str, dict[str, Any]]", data["vulnerabilities"])
    vias = [via for entry in entries.values() for via in cast("list[object]", entry["via"])]
    return any(
        isinstance(via, dict) and cast("dict[str, Any]", via)["severity"] in {"high", "critical"}
        for via in vias
    )


class AuditWorld:
    """Stand-ins for git, uv, pip-audit and npm audit, per side (BASE or HEAD).

    pip-audit and npm audit exit as the real tools do: 1 when they report a finding (npm: at
    HIGH or above), else 0; `rc` overrides that per side. A report of None writes no JSON, and a
    `bytes` report is written as is. BASE's files exist only for the paths in `base_paths`.
    """

    def __init__(self, tmp_path: Path, changed: str) -> None:
        self.root = tmp_path / "root"
        self.work = tmp_path / "work"
        self.root.mkdir()
        self.work.mkdir()
        self.common = tmp_path / "main-checkout" / ".git"
        self.common.mkdir(parents=True)
        self.changed = changed
        self.base_paths = {"pyproject.toml", "uv.lock", f"{Path(changed).parent}/package.json"}
        self.base_paths.add(changed)
        self.reports: dict[str, Any] = {"head": _pip_report(), "base": _pip_report()}
        self.rc: dict[str, int] = {}
        self.calls: list[list[str]] = []

    def git_bytes(self, _root: Path, *args: str, **_kw: object) -> bytes:
        if args[0] == "diff":
            return f"{self.changed}\0".encode()
        if args[0] == "ls-tree":
            return "".join(f"{p}\0" for p in args[4:] if p in self.base_paths).encode()
        if args[0] == "show":
            return b"{}\n"
        if args[0] == "rev-parse":
            return f"{self.common}\n".encode()
        raise AssertionError(args)

    def _side(self, argv: list[str], cwd: Path) -> str:
        if "npm" in argv:
            return "base" if self.work in cwd.parents else "head"
        requirements = argv[argv.index("-r") + 1]
        return "base" if requirements.endswith("-base.txt") else "head"

    def run(self, argv: list[str], cwd: Path, **_: object) -> Any:
        self.calls.append(list(argv))
        completed = subprocess.CompletedProcess
        if "export" in argv:
            Path(argv[argv.index("-o") + 1]).write_text("fixture==1.0\n")
            return completed(argv, 0, b"", b"")
        side = self._side(argv, cwd)
        report = self.reports[side]
        found = _finds(report)
        if "npm" in argv:
            stdout = report if isinstance(report, bytes) else json.dumps(report).encode()
            return completed(argv, self.rc.get(side, int(found)), stdout, b"")
        if report is not None:
            output = Path(argv[argv.index("--output") + 1])
            output.write_bytes(report if isinstance(report, bytes) else json.dumps(report).encode())
        return completed(argv, self.rc.get(side, int(found)), b"", b"pip-audit error\n")

    def check(self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], Any]:
        monkeypatch.setattr(gate, "git_bytes", self.git_bytes)
        monkeypatch.setattr(gate, "_run", self.run)
        ctx = gate.Context(
            root=self.root, base="b", head="h", tree="t", runs_root=self.root, work=self.work
        )
        return gate.check_audit(ctx), ctx


def _details(refusals: list[Any]) -> list[tuple[str, str]]:
    return [(r.code, r.detail) for r in refusals]


def test_pip_audit_ignores_exactly_the_reviewed_advisory_on_both_sides(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert gate.PIP_AUDIT_REVIEWED == (("GHSA-xcq7-p3pf-p88h", "O-OTEL-TRACESTATE-001"),)
    world = AuditWorld(tmp_path, "uv.lock")

    refusals, ctx = world.check(gate, monkeypatch)

    assert refusals == []
    assert ctx.warnings == []
    audits = [argv for argv in world.calls if "pip-audit" in argv]
    assert [argv[argv.index("-r") + 1].rsplit("-", 1)[-1] for argv in audits] == [
        "head.txt",
        "base.txt",
    ]
    for audit in audits:
        assert "--strict" in audit
        assert [audit[i + 1] for i, arg in enumerate(audit) if arg == "--ignore-vuln"] == [
            REVIEWED_VULN
        ]


def test_a_pip_advisory_already_in_base_warns_with_its_register_row(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AuditWorld(tmp_path, "uv.lock")
    world.reports["head"] = _pip_report((OLD_VULN, (OLD_ALIAS,)))
    world.reports["base"] = _pip_report((OLD_VULN, (OLD_ALIAS,)))
    # The worktree case: the local register lives beside the main checkout's .git.
    (world.common.parent / "security-findings.md").write_text(
        f"| ID | Severity |\n|---|---|\n| O-FIXTURE-001 | MEDIUM | pkg, {OLD_VULN}; open |\n"
    )

    refusals, ctx = world.check(gate, monkeypatch)

    assert refusals == []
    assert ctx.warnings == [
        f"pip-audit uv.lock {OLD_VULN} / {OLD_ALIAS} in fixture-package is pre-existing versus "
        "BASE (register row O-FIXTURE-001)"
    ]


def test_a_pre_existing_advisory_without_a_register_row_warns_and_never_refuses(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AuditWorld(tmp_path, "uv.lock")
    world.reports["head"] = _pip_report((OLD_VULN, ()))
    world.reports["base"] = _pip_report((OLD_VULN, ()))

    refusals, ctx = world.check(gate, monkeypatch)

    assert refusals == []
    assert [w.endswith("(no register row)") for w in ctx.warnings] == [True]


def test_a_pip_advisory_new_in_head_refuses_naming_it(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AuditWorld(tmp_path, "uv.lock")
    world.reports["head"] = _pip_report((OLD_VULN, ()), (NEW_VULN, ()))
    world.reports["base"] = _pip_report((OLD_VULN, ()))

    refusals, ctx = world.check(gate, monkeypatch)

    assert _details(refusals) == [
        ("AUDIT_FINDING", f"pip-audit uv.lock {NEW_VULN} in fixture-package is new versus BASE")
    ]
    assert len(ctx.warnings) == 1
    assert OLD_VULN in ctx.warnings[0]


def test_a_head_advisory_matching_only_a_base_alias_is_pre_existing(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A database can re-key an advisory between the two audits; the alias still ties them.
    world = AuditWorld(tmp_path, "uv.lock")
    world.reports["head"] = _pip_report((OLD_ALIAS, ()))
    world.reports["base"] = _pip_report((OLD_VULN, (OLD_ALIAS,)))

    refusals, ctx = world.check(gate, monkeypatch)

    assert refusals == []
    assert len(ctx.warnings) == 1


def test_every_head_advisory_is_new_when_base_has_no_uv_lock(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AuditWorld(tmp_path, "uv.lock")
    world.base_paths -= {"uv.lock"}
    world.reports["head"] = _pip_report((OLD_VULN, ()))
    world.reports["base"] = _pip_report((OLD_VULN, ()))

    refusals, _ = world.check(gate, monkeypatch)

    assert [r.code for r in refusals] == ["AUDIT_FINDING"]
    assert "is new versus BASE" in refusals[0].detail
    assert not any(
        argv[argv.index("-r") + 1].endswith("-base.txt")
        for argv in world.calls
        if "pip-audit" in argv
    )


def test_a_new_npm_high_refuses_and_a_pre_existing_one_warns(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AuditWorld(tmp_path, "infra/package-lock.json")
    world.reports["head"] = _npm_report((NPM_OLD, 101, "high"), (NPM_NEW, 102, "critical"))
    world.reports["base"] = _npm_report((NPM_OLD, 101, "high"))

    refusals, ctx = world.check(gate, monkeypatch)

    lock = "npm audit infra/package-lock.json"
    assert _details(refusals) == [
        ("AUDIT_FINDING", f"{lock} {NPM_NEW} / npm-advisory-102 in fixture-npm is new versus BASE")
    ]
    assert ctx.warnings == [
        f"{lock} {NPM_OLD} / npm-advisory-101 in fixture-npm is pre-existing versus BASE "
        "(no register row)"
    ]


def test_a_new_npm_moderate_is_not_compared(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AuditWorld(tmp_path, "infra/package-lock.json")
    world.reports["head"] = _npm_report((NPM_NEW, 102, "moderate"))
    world.reports["base"] = _npm_report()

    refusals, ctx = world.check(gate, monkeypatch)

    assert refusals == []
    assert ctx.warnings == []


@pytest.mark.parametrize("side", ["head", "base"])
@pytest.mark.parametrize("lockfile", ["uv.lock", "frontend/package-lock.json"])
@pytest.mark.parametrize("failure", ["exit-2", "invalid-json"])
def test_a_scanner_that_cannot_run_on_either_side_refuses(  # noqa: PLR0913, PLR0917
    gate: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    lockfile: str,
    failure: str,
) -> None:
    world = AuditWorld(tmp_path, lockfile)
    if failure == "exit-2":
        world.rc[side] = 2
    else:
        world.reports[side] = b"{not json"

    refusals, _ = world.check(gate, monkeypatch)

    assert [r.code for r in refusals] == ["AUDIT_FINDING"]
    assert " could not run: " in refusals[0].detail


def test_a_scanner_exit_1_with_no_advisory_listed_refuses(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exit 1 with an empty report is a scanner fault (pip-audit --strict on an unresolvable
    # dependency), not a clean audit.
    world = AuditWorld(tmp_path, "uv.lock")
    world.rc["head"] = 1

    refusals, _ = world.check(gate, monkeypatch)

    assert [r.code for r in refusals] == ["AUDIT_FINDING"]
    assert "exited 1 but listed no advisory" in refusals[0].detail


@needs_gitleaks
def test_warnings_print_after_ok_are_recorded_and_never_change_the_exit_code(
    gate: ModuleType, repo: Repo
) -> None:
    def warns(ctx: Any) -> list[Any]:
        ctx.warnings.append(f"pip-audit uv.lock {OLD_VULN} is pre-existing versus BASE")
        return []

    repo.commit({"docs/notes.md": "public notes\n"})
    checks = (gate.Check("audit", "AUDIT_FINDING", warns),)
    lines: list[str] = []

    code = gate.run_gate(repo.path, repo.base, checks, out=lines.append)

    assert code == 0, lines
    assert lines[lines.index("publication-gate: audit ok") + 1] == (
        f"publication-gate: audit WARN pip-audit uv.lock {OLD_VULN} is pre-existing versus BASE"
    )
    steps = json.loads((_gate_record(repo) / "steps.json").read_text())
    assert [(s["exit_code"], s["warnings"]) for s in steps if s["name"] == "audit"] == [
        (0, [f"pip-audit uv.lock {OLD_VULN} is pre-existing versus BASE"])
    ]


def test_every_reviewed_advisory_names_a_dispositions_row(gate: ModuleType) -> None:
    dispositions = (SCRIPTS.parent / "docs" / "security-dispositions.md").read_text("utf-8")
    for vuln, row in gate.PIP_AUDIT_REVIEWED:
        line = next(
            (entry for entry in dispositions.splitlines() if entry.startswith(f"| {row} |")), ""
        )
        assert "NOT AFFECTED" in line
        assert vuln in line
