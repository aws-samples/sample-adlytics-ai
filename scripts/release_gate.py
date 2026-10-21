#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Refuse a release unless HEAD passes the clean-export gates and a full ASH container scan.

    make release-gate    (uv run python scripts/release_gate.py)

Run on a clean checkout of a pull request's head, before `make merge-pr`, which refuses a merge
without this gate's sealed passing record of the head's tree. It takes about an hour. Every
stage prints one line per result, and a refusal names its code:

    preconditions (stop the gate)
      DIRTY_TREE             uncommitted or untracked changes: the export would not hold them
      TOOL_VERSION_MISMATCH  the CONTRIBUTING.md Maintainer tools table has no ASH pin
      TOOL_MISSING           neither finch nor docker, or no uvx, is on PATH
    stages (every one runs, whatever an earlier one found)
      VERIFY_CLEAN_FAILED    make verify-clean failed (it runs only when no sealed, passing,
                             canonical, clean verify-clean record of HEAD's tree can be reused)
      ASH_RUN_FAILED         the export, the template export or an ASH run exited non-zero
      ASH_SCANNER_PROBLEM    a scanner reported MISSING or ERROR, a requested scanner did not run,
                             a flat report is unreadable, or ASH is not the pinned version
      ASH_OPEN_FINDING       an open CRITICAL or HIGH finding after the committed suppressions
      CHECKOUT_CHANGED       HEAD, its tree or the working tree changed while the gate ran
    report only (never refuses, never changes the exit code)
      mutation-summary       `scripts/mutation.py status --summary`; a failure is a warning

The ASH stage scans a `git archive HEAD` export three times in container mode: trivy-repo, ASH's
default source scanners, and the synthesised templates (infra/scripts/export-scan-templates.ts)
with cdk-nag, cfn-nag, checkov and detect-secrets. ASH, the template export and the mutation
summary run with `scan_env()`: no ADLYTICS_* or AWS_* variable and nothing else the container
runtime forwards into its VM, because scanning a tree needs no AWS access. Findings are printed as
scanner, severity, rule, file and count, never their content. The gate writes a `release-gate` run
record holding every step, the ASH summaries, the three ASH `reports/` directories and the child
records.
Touches no AWS.
"""

from __future__ import annotations

import collections
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

SCRIPTS = Path(__file__).resolve().parent
CONTRIBUTING = SCRIPTS.parent / "CONTRIBUTING.md"
KIND = "release-gate"
ASH_TOOL = "ASH (automated-security-helper)"
ASH_SOURCE = "git+https://github.com/awslabs/automated-security-helper.git"
CONTAINER_RUNTIMES = ("finch", "docker")
REFUSED_SEVERITIES = ("CRITICAL", "HIGH")
PROBLEM_STATUSES = ("MISSING", "ERROR")
FLAT_REPORT = Path("reports") / "ash.flat.json"
MUTATION_SUMMARY = ("uv", "run", "python", "scripts/mutation.py", "status", "--summary")
VERIFY_CLEAN_KIND = "verify-clean"
EXPORT_IDENTITY = (
    "-c",
    "user.name=release-gate",
    "-c",
    "user.email=release-gate@invalid",
    "-c",
    "commit.gpgsign=false",
)
EXIT_REFUSED = 1
# Removed from every ASH, template-export and mutation-summary environment: deploy flags, and every
# AWS variable, since a scan needs no AWS access.
SCRUBBED_PREFIXES = ("ADLYTICS_", "AWS_")
# The variables the Finch CLI forwards into its VM on every nerdctl call when they are set
# (runfinch/finch v1.17.2, cmd/finch/nerdctl_remote.go); the AWS_* ones are covered by the prefix.
# Finch is how the operator's AWS_PROFILE reached the scan's container runtime.
CONTAINER_FORWARDED = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_ECR_DISABLE_CACHE",
    "AWS_ECR_CACHE_DIR",
    "AWS_ECR_IGNORE_CREDS_STORAGE",
    "COSIGN_PASSWORD",
    "COMPOSE_FILE",
    "SOURCE_DATE_EPOCH",
    "DOCKER_CONFIG",
)


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
    """A command a stage depends on failed; the stage refuses rather than passing unexamined."""


class Runner(Protocol):
    """Runs one external command; streams and captures it, or writes it to `log` when given."""

    def __call__(  # noqa: PLR0913 - argv and cwd, then keyword-only env and log
        self, argv: Sequence[str], cwd: Path, *, env: dict[str, str], log: Path | None = None
    ) -> tuple[int, bytes]:
        """Return the exit code and the captured output (empty when written to `log`)."""
        ...


def default_runner(  # noqa: PLR0913 - argv and cwd, then keyword-only env and log
    argv: Sequence[str], cwd: Path, *, env: dict[str, str], log: Path | None = None
) -> tuple[int, bytes]:
    """Stream and capture the command (no log), or append its output to `log`."""
    if log is None:
        return cast("tuple[int, bytes]", run_record.run_child(list(argv), cwd, env))
    with log.open("ab") as handle:
        try:
            # A-SUBPROCESS-001: fixed tool argv built here; see docs/security-dispositions.md
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            completed = subprocess.run(  # noqa: S603
                list(argv), cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False
            )
        except OSError as error:
            handle.write(f"release-gate: {argv[0]} could not start: {error}\n".encode())
            return 127, b""
    return completed.returncode, b""


@dataclass(frozen=True)
class Refusal:
    """One finding: the refusal code and a detail that never holds a finding's content."""

    code: str
    detail: str


@dataclass
class Result:
    """What one precondition or stage found."""

    name: str
    code: str
    refusals: list[Refusal]
    duration_s: float = 0.0
    child_records: list[str] = field(default_factory=list[str])
    # verify-clean only: the sealed record reused instead of running the stage, as <kind>/<dir>.
    reused: str | None = None


@dataclass(frozen=True)
class AshRun:
    """One ASH scan: its name, the scanners it requests (none = ASH's default set), extra args."""

    name: str
    scanners: tuple[str, ...]
    extra: tuple[str, ...] = ()


ASH_RUNS: tuple[AshRun, ...] = (
    AshRun(
        "trivy",
        ("trivy-repo",),
        (
            "--config-overrides",
            'ash_plugin_modules=["automated_security_helper.plugin_modules.ash_trivy_plugins"]',
        ),
    ),
    AshRun("source", ()),
    AshRun("templates", ("cdk-nag", "cfn-nag", "checkov", "detect-secrets")),
)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def _run(
    argv: Sequence[str], cwd: Path, *, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        # A-SUBPROCESS-001: fixed git/tar argv built here; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        return subprocess.run(  # noqa: S603
            list(argv), cwd=cwd, input=stdin, capture_output=True, check=False
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


def _status_lines(root: Path) -> list[str]:
    status = git_bytes(root, "status", "--porcelain", "--untracked-files=all")
    return [line for line in status.decode("utf-8", "replace").splitlines() if line.strip()]


def scan_env() -> dict[str, str]:
    """Return the environment for ASH, the template export and the mutation summary.

    It carries no ADLYTICS_* deploy flag (a reader's shell has none), no AWS_* variable, and no
    other variable the container runtime forwards into its VM (CONTAINER_FORWARDED).
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(SCRUBBED_PREFIXES) and k not in CONTAINER_FORWARDED
    }


def ash_argv(  # noqa: PLR0913, PLR0917 - the pin, the run and its two directories
    version: str, run: AshRun, source: Path, output: Path
) -> list[str]:
    """Return the ASH command for one run, built from the pinned version."""
    argv = [
        "uvx",
        "--from",
        f"{ASH_SOURCE}@v{version}",
        "ash",
        "scan",
        "--mode",
        "container",
        "--source-dir",
        str(source),
        "--output-dir",
        str(output),
        "--no-progress",
        "--no-fail-on-findings",
    ]
    if run.scanners:
        argv += ["--scanners", ",".join(run.scanners)]
    return [*argv, *run.extra]


# --------------------------------------------------------------------------------------------
# The flat-report summary
# --------------------------------------------------------------------------------------------


def _objects(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return [cast("dict[str, Any]", v) for v in cast("list[object]", value) if isinstance(v, dict)]


def summarize(
    flat: dict[str, Any], *, requested: Sequence[str] = (), version: str | None = None
) -> dict[str, Any]:
    """Summarize one ASH flat report: counts, open CRITICAL/HIGH rows, and scanner problems.

    A finding counts as suppressed only when `is_suppressed` is exactly true. A scanner problem is
    a MISSING or ERROR status (or missing dependencies), a requested scanner that is absent or
    SKIPPED, or a report from another ASH version. Raises ValueError on a malformed report.
    """
    findings = _objects(flat.get("findings"))
    metrics = _objects(flat.get("scanner_metrics"))
    counts: collections.Counter[str] = collections.Counter()
    open_rows: collections.Counter[tuple[str, str, str, str]] = collections.Counter()
    for finding in findings:
        scanner = str(finding.get("scanner", "unknown"))
        severity = str(finding.get("severity", "UNKNOWN")).upper()
        suppressed = finding.get("is_suppressed") is True
        counts[f"{scanner}|{severity}|{'suppressed' if suppressed else 'open'}"] += 1
        if not suppressed and severity in REFUSED_SEVERITIES:
            rule = str(finding.get("rule_id", "unknown-rule"))
            path = str(finding.get("file_path", "?"))
            open_rows[(scanner, severity, rule, path)] += 1

    statuses: dict[str, str] = {}
    problems: list[str] = []
    for metric in metrics:
        name = str(metric.get("scanner_name", "unknown"))
        status = str(metric.get("status", "UNKNOWN")).upper()
        statuses[name] = status
        if status in PROBLEM_STATUSES:
            problems.append(f"{name} {status}")
        elif metric.get("dependencies_missing") is True:
            problems.append(f"{name} MISSING (dependencies missing)")
    for name in requested:
        if name not in statuses:
            problems.append(f"{name} requested but absent from the report")
        elif statuses[name] == "SKIPPED":
            problems.append(f"{name} requested but SKIPPED")
    reported = cast("dict[str, Any]", flat.get("metadata") or {}).get("tool_version")
    if version is not None and reported != version:
        problems.append(f"the report is from ASH {reported}, not the pinned {version}")

    return {
        "total": len(findings),
        "scanners": dict(sorted(statuses.items())),
        "by_scanner_severity_state": dict(sorted(counts.items())),
        "open_critical_high": [
            {"scanner": s, "severity": sev, "rule_id": r, "file_path": p, "count": n}
            for (s, sev, r, p), n in sorted(open_rows.items())
        ],
        "scanner_problems": problems,
    }


def summary_refusals(name: str, summary: dict[str, Any]) -> list[Refusal]:
    """Return the refusals one run's summary carries: scanner problems, then open rows."""
    refusals = [Refusal("ASH_SCANNER_PROBLEM", f"{name}: {p}") for p in summary["scanner_problems"]]
    refusals += [
        Refusal(
            "ASH_OPEN_FINDING",
            f"{name}: {row['scanner']} {row['severity']} {row['rule_id']} "
            f"{row['file_path']} x{row['count']}",
        )
        for row in summary["open_critical_high"]
    ]
    return refusals


# --------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------


@dataclass
class Context:
    """What every stage reads: the checkout, its state at the start, and the injected runner."""

    root: Path
    head: str
    tree: str
    runs: Path
    ash_version: str
    runner: Runner
    env: dict[str, str]
    ash: dict[str, Any] = field(default_factory=dict[str, Any])
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    # The live record's `<kind>/<dir>`, and the heartbeat step setter (docs/test-run-records.md).
    record: str | None = None
    on_step: Callable[[str], None] = lambda _step: None


def _child_names(directory: Path) -> list[str] | None:
    """Return a record's child records, or None when one is not a `<kind>/<record>` name."""
    run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    children = cast("dict[str, Any]", run).get("child_records")
    if not isinstance(children, list):
        return None
    names = [str(c) for c in cast("list[object]", children)]
    for name in names:
        kind, _, record = name.partition("/")
        if not (run_record.KIND_NAME.match(kind) and run_record.RECORD_NAME.match(record)):
            return None
    return names


def reusable_verify_clean(records: Path, tree: str) -> tuple[Path, list[str]] | None:
    """Return the newest reusable verify-clean record of `tree` and its child records, or None.

    Reusable: sealed and verified by its manifest, passing, canonical and clean, with at least one
    child record, each of which verifies too.
    """
    for directory in run_record.find_records(
        records, VERIFY_CLEAN_KIND, tree=tree, passing=True, canonical=True, clean=True
    ):
        children = _child_names(directory)
        if children and not any(run_record.verify_record(records / c) for c in children):
            return directory, children
    return None


def stage_verify_clean(ctx: Context) -> Result:
    """Reuse the verify-clean record of HEAD's tree, or run make verify-clean to write one."""
    # make verify-clean and its clean export write into <root>/.runs, whatever RUN_RECORD_ROOT says.
    records = ctx.root / ".runs"
    found = reusable_verify_clean(records, ctx.tree)
    if found is not None:
        directory, children = found
        name = f"{VERIFY_CLEAN_KIND}/{directory.name}"
        return Result(
            "verify-clean",
            "VERIFY_CLEAN_FAILED",
            [],
            child_records=sorted([name, *children]),
            reused=name,
        )
    before = set(run_record.record_dirs(records))
    env = {**os.environ, run_record.ROOT_VARIABLE: str(records)}
    if ctx.record is not None:
        env[run_record.PARENT_VARIABLE] = ctx.record
    argv = [sys.executable, str(SCRIPTS / "run_record.py"), "gate", "--kind", VERIFY_CLEAN_KIND]
    code, _ = ctx.runner(argv, ctx.root, env=env)
    children = sorted(
        p.relative_to(records).as_posix() for p in set(run_record.record_dirs(records)) - before
    )
    refusals = (
        [] if code == 0 else [Refusal("VERIFY_CLEAN_FAILED", f"make verify-clean exited {code}")]
    )
    return Result("verify-clean", "VERIFY_CLEAN_FAILED", refusals, child_records=children)


def export_head(ctx: Context, export: Path) -> None:
    """Write `git archive HEAD` to `export` and index it as a one-commit repository."""
    archive = git_bytes(ctx.root, "archive", "--format=tar", ctx.head)
    extracted = _run(["tar", "-x", "-C", str(export)], export, stdin=archive)
    if extracted.returncode != 0:
        raise GateError(f"tar exited {extracted.returncode}: {_last_line(extracted.stderr)}")
    git_bytes(export, "init", "-q")
    git_bytes(export, "add", "-A")
    git_bytes(export, *EXPORT_IDENTITY, "commit", "-q", "-m", "release-gate export")
    if git_text(export, "rev-parse", "HEAD^{tree}") != ctx.tree:
        raise GateError("the export's tree differs from HEAD^{tree}")


def _scan(  # noqa: PLR0913, PLR0917 - the context, the run and its two directories
    ctx: Context, run: AshRun, source: Path, out: Path
) -> list[Refusal]:
    """Run one ASH scan and summarize its flat report."""
    ctx.on_step(f"ash/{run.name}")
    output = out / run.name
    code, _ = ctx.runner(
        ash_argv(ctx.ash_version, run, source, output),
        source,
        env=ctx.env,
        log=out / f"{run.name}.log",
    )
    if code != 0:
        return [Refusal("ASH_RUN_FAILED", f"{run.name}: ash exited {code}")]
    try:
        flat = json.loads((output / FLAT_REPORT).read_text(encoding="utf-8"))
        if not isinstance(flat, dict):
            raise ValueError("not a JSON object")
        summary = summarize(
            cast("dict[str, Any]", flat), requested=run.scanners, version=ctx.ash_version
        )
    except (OSError, ValueError) as error:
        return [Refusal("ASH_SCANNER_PROBLEM", f"{run.name}: {FLAT_REPORT} unreadable: {error}")]
    ctx.ash[run.name] = summary
    return summary_refusals(run.name, summary)


def _export_templates(ctx: Context, export: Path, out: Path) -> Path:
    """Install infra, build the Lambdas and export every template outside the export."""
    ctx.on_step("ash/export-templates")
    log = out / "export-templates.log"
    templates = out / "exported-templates"
    steps: list[tuple[list[str], Path]] = [
        (["npm", "ci"], export / "infra"),
        (["make", "build-lambdas"], export),
        (["npx", "ts-node", "scripts/export-scan-templates.ts", str(templates)], export / "infra"),
    ]
    for argv, cwd in steps:
        code, _ = ctx.runner(argv, cwd, env=ctx.env, log=log)
        if code != 0:
            raise GateError(f"template export: {' '.join(argv[:2])} exited {code}")
    return templates


def _attach(ctx: Context, out: Path) -> None:
    """Copy every run's reports/ directory and every log into the record's raw/ash/."""
    for run in ASH_RUNS:
        reports = out / run.name / "reports"
        if reports.is_dir():
            for path in sorted(reports.rglob("*")):
                if path.is_file():
                    rel = path.relative_to(out).as_posix()
                    ctx.files[f"raw/ash/{rel}"] = path.read_bytes()
    for log in sorted(out.glob("*.log")):
        ctx.files[f"raw/ash/{log.name}"] = log.read_bytes()


def stage_ash(ctx: Context) -> Result:
    """Scan a HEAD export with ASH three times; refuse on a failure, a problem or an open row."""
    # Under the record root (inside the home directory) rather than the system temp directory,
    # which a container runtime VM may not share; .runs/ is gitignored and the staging is removed.
    parent = ctx.runs / ".staging"
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="release-gate-", dir=parent))
    refusals: list[Refusal] = []
    try:
        export, out = staging / "export", staging / "ash"
        export.mkdir()
        out.mkdir()
        try:
            export_head(ctx, export)
            trivy, source, templates_run = ASH_RUNS
            refusals += _scan(ctx, trivy, export, out)
            refusals += _scan(ctx, source, export, out)
            templates = _export_templates(ctx, export, out)
            refusals += _scan(ctx, templates_run, templates, out)
        except (GateError, OSError) as error:
            refusals.append(Refusal("ASH_RUN_FAILED", f"the ASH stage could not run: {error}"))
        _attach(ctx, out)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return Result("ash", "ASH_RUN_FAILED", refusals)


def stage_checkout(ctx: Context) -> Result:
    """Refuse when HEAD, its tree or the working tree changed while the gate ran."""
    refusals: list[Refusal] = []
    head = git_text(ctx.root, "rev-parse", "HEAD")
    tree = git_text(ctx.root, "rev-parse", "HEAD^{tree}")
    if head != ctx.head:
        refusals.append(Refusal("CHECKOUT_CHANGED", f"HEAD moved {ctx.head[:12]} -> {head[:12]}"))
    if tree != ctx.tree:
        refusals.append(Refusal("CHECKOUT_CHANGED", f"tree changed {ctx.tree[:12]} -> {tree[:12]}"))
    dirty = _status_lines(ctx.root)
    if dirty:
        refusals.append(Refusal("CHECKOUT_CHANGED", f"{len(dirty)} path(s) changed or appeared"))
    return Result("checkout", "CHECKOUT_CHANGED", refusals)


@dataclass(frozen=True)
class Stage:
    """A named stage and the code it refuses with when it cannot run at all."""

    name: str
    code: str
    run: Callable[[Context], Result]


STAGES: tuple[Stage, ...] = (
    Stage("verify-clean", "VERIFY_CLEAN_FAILED", stage_verify_clean),
    Stage("ash", "ASH_RUN_FAILED", stage_ash),
    Stage("checkout", "CHECKOUT_CHANGED", stage_checkout),
)


def mutation_summary(root: Path, runner: Runner, env: dict[str, str]) -> dict[str, Any]:
    """Run the mutation coverage summary; report only, so nothing it does can refuse the gate."""
    start = time.monotonic()
    try:
        code, console = runner(list(MUTATION_SUMMARY), root, env=env)
    except Exception as error:  # noqa: BLE001 - report only: no failure here may change the gate
        code, console = None, b""
        warning: str | None = f"could not run: {type(error).__name__}: {error}"
    else:
        warning = None if code == 0 else f"exited {code}"
    return {
        "exit_code": code,
        "warning": warning,
        "text": console.decode("utf-8", "replace"),
        "duration_s": round(time.monotonic() - start, 6),
    }


# --------------------------------------------------------------------------------------------
# Preconditions, output and the record
# --------------------------------------------------------------------------------------------


def preconditions(
    root: Path, contributing: Path, which: Callable[[str], str | None]
) -> tuple[str | None, str | None, list[Result]]:
    """Return the ASH pin, the container runtime found (or None) and the precondition results."""
    results: list[Result] = []
    try:
        dirty = _status_lines(root)
        clean = (
            [Refusal("DIRTY_TREE", f"{len(dirty)} uncommitted or untracked path(s)")]
            if dirty
            else []
        )
    except GateError as error:
        clean = [Refusal("DIRTY_TREE", str(error))]
    results.append(Result("clean-tree", "DIRTY_TREE", clean))

    version: str | None = None
    try:
        version = cast("str", tool_versions.pinned(ASH_TOOL, contributing))
        pin: list[Refusal] = []
    except tool_versions.ToolVersionError as error:
        pin = [Refusal("TOOL_VERSION_MISMATCH", str(error))]
    results.append(Result("ash-pin", "TOOL_VERSION_MISMATCH", pin))

    runtime = next((name for name in CONTAINER_RUNTIMES if which(name)), None)
    missing: list[Refusal] = []
    if runtime is None:
        missing.append(Refusal("TOOL_MISSING", "neither finch nor docker is on PATH"))
    if which("uvx") is None:
        missing.append(Refusal("TOOL_MISSING", "uvx is not on PATH"))
    results.append(Result("tools", "TOOL_MISSING", missing))
    return version, runtime, results


def _lines(stage: str, result: Result) -> list[str]:
    label = f"{stage}{result.name}"
    if not result.refusals:
        return [f"release-gate: {label} ok"]
    return [f"release-gate: {label} REFUSED {r.code} {r.detail}" for r in result.refusals]


def _utc(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _steps(results: Sequence[Result], mutation: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "name": r.name,
            "code": r.code,
            "exit_code": EXIT_REFUSED if r.refusals else 0,
            "duration_s": r.duration_s,
            "refusals": [{"code": f.code, "detail": f.detail} for f in r.refusals],
            "child_records": r.child_records,
            **({"reused": r.reused} if r.reused is not None else {}),
        }
        for r in results
    ]
    steps.append(
        {
            "name": "mutation-summary",
            "code": None,
            "report_only": True,
            "exit_code": mutation["exit_code"] if mutation["exit_code"] is not None else -1,
            "duration_s": mutation["duration_s"],
            "warning": mutation["warning"],
            "refusals": [],
            "child_records": [],
        }
    )
    return steps


def _run_stage(ctx: Context, stage: Stage, say: Callable[[str], None]) -> Result:
    """Run one stage; a stage that cannot run at all refuses with its own code."""
    say(f"release-gate: {stage.name} start")
    ctx.on_step(stage.name)
    start = time.monotonic()
    try:
        result = stage.run(ctx)
    except (GateError, OSError, ValueError) as error:
        detail = f"the stage could not run: {error}"
        result = Result(stage.name, stage.code, [Refusal(stage.code, detail)])
    result.duration_s = round(time.monotonic() - start, 6)
    if result.reused is not None:
        say(f"release-gate: {stage.name} reused {result.reused}")
    for line in _lines("", result):
        say(line)
    return result


def run_gate(  # noqa: PLR0913 - the injection points the tests use, all keyword-only
    root: Path,
    *,
    runner: Runner = default_runner,
    which: Callable[[str], str | None] = shutil.which,
    runs_root: Path | None = None,
    contributing: Path = CONTRIBUTING,
    out: Callable[[str], None] = print,
) -> int:
    """Run the preconditions, every stage and the mutation summary; record it as it runs."""
    started, utc_start = time.monotonic(), datetime.now(UTC)
    runs = cast("Path", run_record.record_root(runs_root, cwd=root))
    porcelain_before = run_record.porcelain_count(root)
    head = git_text(root, "rev-parse", "HEAD")
    tree = git_text(root, "rev-parse", "HEAD^{tree}")
    record: dict[str, Any] = {
        "canonical": runner is default_runner and contributing == CONTRIBUTING,
        "argv": ["release_gate.py"],
        "cwd": ".",
        "head": head,
        "tree": tree,
        "branch": git_text(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "source_commit": head,
        "context": "checkout",
        "utc_start": _utc(utc_start),
        "porcelain_before": porcelain_before,
    }
    stages_done: list[str] = []
    try:
        live = run_record.LiveRecord.begin(
            KIND,
            record,
            root=runs,
            now=utc_start,
            progress=lambda: {"stages_done": len(stages_done), "stages_total": len(STAGES)},
        )
    except (run_record.RunRecordError, OSError) as error:
        out(f"release-gate: REFUSED RECORD_NOT_WRITTEN {error}")
        return EXIT_REFUSED

    def say(line: str) -> None:
        out(line)
        live.line(line)

    live.set_step("preconditions")
    version, runtime, results = preconditions(root, contributing, which)
    for result in results:
        for line in _lines("precondition ", result):
            say(line)

    ctx = Context(
        root,
        head,
        tree,
        runs,
        version or "",
        runner,
        scan_env(),
        record=live.name,
        on_step=live.set_step,
    )
    if version is not None and not any(r.refusals for r in results):
        for stage in STAGES:
            results.append(_run_stage(ctx, stage, say))
            stages_done.append(stage.name)

    # Report only: whatever the summary does, `refused` below is computed without it.
    live.set_step("mutation-summary")
    mutation = mutation_summary(root, runner, ctx.env)
    if mutation["warning"] is None:
        say("release-gate: mutation-summary reported (report only)")
    else:
        say(
            f"release-gate: mutation-summary WARNING {mutation['warning']} "
            "(report only; the gate result is unchanged)"
        )
    live.set_step(None)

    refused = sorted({f.code for r in results for f in r.refusals})
    exit_code = EXIT_REFUSED if refused else 0
    porcelain_after = run_record.porcelain_count(root)
    verdict = f"REFUSED {' '.join(refused)}" if refused else "passed"
    summary = f"release-gate: {verdict} for HEAD {head[:12]}"
    record.update(
        {
            "utc_end": _utc(datetime.now(UTC)),
            "duration_s": round(time.monotonic() - started, 6),
            "porcelain_after": porcelain_after,
            "clean": porcelain_before == 0 and porcelain_after == 0,
            "exit_code": exit_code,
            "outcome": "fail" if refused else "pass",
            "report_missing": False,
            "refused": refused,
            "tool_versions": {
                **run_record.tool_versions(root, None),
                "ash": version,
                "container_runtime": runtime,
            },
            "ash": ctx.ash,
            "mutation_summary": mutation,
            "child_records": [c for r in results for c in r.child_records],
            "verify_clean_reused": next((r.reused for r in results if r.reused), None),
        }
    )
    live.line(summary)
    try:
        directory = live.finalise(record, steps=_steps(results, mutation), files=ctx.files)
    except (run_record.RunRecordError, OSError) as error:
        out(f"release-gate: REFUSED RECORD_NOT_WRITTEN {error}")
        return EXIT_REFUSED
    out(summary)
    out(f"run-record: {directory}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Run the full gate on the repository containing the working directory."""
    args = sys.argv[1:] if argv is None else argv
    if args:
        sys.stderr.write("usage: release_gate.py (no arguments; no flag skips a stage)\n")
        return 2
    top = run_record.toplevel(Path.cwd())
    if top is None:
        sys.stderr.write("release-gate: not in a git repository\n")
        return 2
    try:
        return run_gate(top)
    except GateError as error:
        sys.stderr.write(f"release-gate: REFUSED {error}\n")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
