# SPDX-License-Identifier: MIT-0
"""The release gate refuses an open CRITICAL/HIGH or a broken scanner, and nothing else blocks it.

`scripts/release_gate.py` runs once per release: verify-clean, three ASH container scans of the
HEAD export, then a report-only mutation coverage summary. A failure here means a release that
ships an open high-severity finding or a scan that silently did not run, or a mutation-coverage
problem that blocks (or masks) a release the owner decided mutation testing never gates.

The full gate (about an hour, needs ASH and a container runtime) is never run here: the external
commands go through an injected runner that writes small flat reports, while git, the export and
the run record are real. Global and system git configuration are replaced with an empty file, so
the operator's hooks and identity never decide a test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from gate_script_support import SCRIPTS, commit, git

pytestmark = pytest.mark.unit

# A synthetic pin for the fixture repository. The real one lives only in CONTRIBUTING.md's
# Maintainer tools table; tests that read it derive their expectations from the parsed value.
PIN = "9.8.7"
CONTRIBUTING = (
    "# Contributing\n\n### Maintainer tools\n\n"
    "| Tool | Version | Used by |\n|---|---|---|\n"
    f"| ASH (automated-security-helper) | {PIN} | make release-gate |\n"
    "| finch or Docker | any current | make release-gate (ASH container mode) |\n\n### Next\n"
)
# A unique marker in every synthetic finding's code_snippet. The gate must never echo snippet
# content, whatever its shape, so a neutral marker proves that as well as a secret-shaped one.
SNIPPET = "snippet-body-the-gate-must-never-print"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    """Load the gate script as a module; it loads the recorder and tool pins beside it."""
    loaded = sys.modules.get("release_gate")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("release_gate", SCRIPTS / "release_gate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _metric(name: str, status: str = "PASSED", *, missing: bool = False) -> dict[str, Any]:
    return {"scanner_name": name, "status": status, "dependencies_missing": missing}


def _finding(severity: str, *, suppressed: bool = False, scanner: str = "bandit") -> dict[str, Any]:
    return {
        "scanner": scanner,
        "severity": severity,
        "is_suppressed": suppressed,
        "rule_id": "B999",
        "file_path": "src/example.py",
        "code_snippet": SNIPPET,
    }


def _flat(
    findings: Sequence[dict[str, Any]],
    metrics: Sequence[dict[str, Any]] = (),
    version: str = PIN,
) -> dict[str, Any]:
    return {
        "metadata": {"tool_version": version},
        "scanner_metrics": list(metrics) or [_metric("bandit")],
        "findings": list(findings),
    }


def _refusals(gate: ModuleType, flat: dict[str, Any], requested: Sequence[str] = ()) -> list[Any]:
    summary = gate.summarize(flat, requested=requested, version=PIN)
    return gate.summary_refusals("source", summary)


def test_an_open_high_finding_refuses_without_printing_its_content(gate: ModuleType) -> None:
    refusals = _refusals(gate, _flat([_finding("HIGH"), _finding("HIGH")]))

    assert [(r.code, r.detail) for r in refusals] == [
        ("ASH_OPEN_FINDING", "source: bandit HIGH B999 src/example.py x2")
    ]
    assert SNIPPET not in repr(refusals)


def test_open_critical_refuses_and_a_missing_flag_counts_as_open(gate: ModuleType) -> None:
    finding = _finding("critical")
    del finding["is_suppressed"]

    assert [r.code for r in _refusals(gate, _flat([finding]))] == ["ASH_OPEN_FINDING"]


def test_a_suppressed_high_and_an_open_medium_pass(gate: ModuleType) -> None:
    flat = _flat([_finding("HIGH", suppressed=True), _finding("MEDIUM"), _finding("LOW")])
    summary = gate.summarize(flat, version=PIN)

    assert gate.summary_refusals("source", summary) == []
    assert summary["total"] == 3
    assert summary["by_scanner_severity_state"] == {
        "bandit|HIGH|suppressed": 1,
        "bandit|LOW|open": 1,
        "bandit|MEDIUM|open": 1,
    }


@pytest.mark.parametrize(
    ("metric", "requested", "problem"),
    [
        (_metric("grype", "MISSING"), (), "grype MISSING"),
        (_metric("semgrep", "ERROR"), (), "semgrep ERROR"),
        (_metric("syft", missing=True), (), "syft MISSING (dependencies missing)"),
        (_metric("trivy-repo", "SKIPPED"), ("trivy-repo",), "trivy-repo requested but SKIPPED"),
        (_metric("bandit"), ("checkov",), "checkov requested but absent from the report"),
    ],
)
def test_a_scanner_that_did_not_run_refuses(  # noqa: PLR0913, PLR0917 -- one parametrized row
    gate: ModuleType, metric: dict[str, Any], requested: tuple[str, ...], problem: str
) -> None:
    refusals = _refusals(gate, _flat([], [metric]), requested)

    assert [(r.code, r.detail) for r in refusals] == [("ASH_SCANNER_PROBLEM", f"source: {problem}")]


def test_skipped_scanners_outside_the_request_and_a_failed_medium_pass(gate: ModuleType) -> None:
    metrics = [_metric("bandit", "SKIPPED"), _metric("cfn-nag", "FAILED")]
    flat = _flat([_finding("MEDIUM", scanner="cfn-nag")], metrics)

    assert _refusals(gate, flat, ("cfn-nag",)) == []


def test_a_report_from_another_ash_version_refuses(gate: ModuleType) -> None:
    refusals = _refusals(gate, _flat([], version="9.8.6"))

    assert [r.code for r in refusals] == ["ASH_SCANNER_PROBLEM"]
    assert f"not the pinned {PIN}" in refusals[0].detail


def test_a_malformed_report_fails_loudly(gate: ModuleType) -> None:
    with pytest.raises(ValueError, match="JSON array"):
        gate.summarize({"findings": {}}, version=PIN)


def test_the_ash_argv_is_built_from_the_pinned_version(gate: ModuleType) -> None:
    version = gate.tool_versions.pinned(gate.ASH_TOOL)
    trivy, source, templates = gate.ASH_RUNS
    source_dir, out = Path("/export"), Path("/out")

    argv = gate.ash_argv(version, source, source_dir, out)
    assert gate.tool_versions.VERSION.fullmatch(version)
    assert argv == [
        "uvx",
        "--from",
        f"git+https://github.com/awslabs/automated-security-helper.git@v{version}",
        "ash",
        "scan",
        "--mode",
        "container",
        "--source-dir",
        "/export",
        "--output-dir",
        "/out",
        "--no-progress",
        "--no-fail-on-findings",
    ]
    trivy_argv = gate.ash_argv(version, trivy, source_dir, out)
    assert trivy_argv[-4:] == [
        "--scanners",
        "trivy-repo",
        "--config-overrides",
        'ash_plugin_modules=["automated_security_helper.plugin_modules.ash_trivy_plugins"]',
    ]
    templates_argv = gate.ash_argv(version, templates, source_dir, out)
    assert templates_argv[-2:] == ["--scanners", "cdk-nag,cfn-nag,checkov,detect-secrets"]


# --------------------------------------------------------------------------------------------
# The whole gate, with the external commands injected
# --------------------------------------------------------------------------------------------


def _make_writable(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        Path(current).chmod(0o755)
        for name in dirs:
            (Path(current) / name).chmod(0o755)
        for name in files:
            (Path(current) / name).chmod(0o644)


class FakeTools:
    """Stands in for verify-clean, npm, make, npx, ASH and the mutation summary."""

    def __init__(self, gate: ModuleType, repo: Path, flats: dict[str, dict[str, Any]]) -> None:
        self.gate = gate
        self.repo = repo
        self.flats = flats
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.mutation: Callable[[], tuple[int, bytes]] = lambda: (0, b"mutation coverage\n")

    def __call__(  # noqa: PLR0913 - the Runner signature
        self,
        argv: Sequence[str],
        cwd: Path,  # noqa: ARG002 - part of the Runner signature
        *,
        env: dict[str, str],
        log: Path | None = None,
    ) -> tuple[int, bytes]:
        """Record the call and act like the tool it names."""
        args = list(argv)
        self.calls.append(args)
        self.envs.append(dict(env))
        verify_clean = _is_verify_clean(args)
        # make verify-clean scrubs ADLYTICS_* itself, in its clean export.
        assert verify_clean or not any(name.startswith("ADLYTICS_") for name in env)
        if log is not None:
            log.write_text(f"{' '.join(args[:2])}\n")
        if verify_clean:
            # make verify-clean writes its record and its children into <root>/.runs.
            assert env["RUN_RECORD_ROOT"] == str(self.repo / ".runs")
            _write_verify_clean(self.gate, self.repo)
            return 0, b"verify-clean: passed\n"
        if args[0] == "uvx":
            out = Path(args[args.index("--output-dir") + 1])
            (out / "reports").mkdir(parents=True)
            (out / "reports" / "ash.flat.json").write_text(json.dumps(self.flats[out.name]))
            return 0, b""
        if args[:2] == ["npx", "ts-node"]:
            Path(args[-1]).mkdir(parents=True)
            return 0, b""
        if args[:2] == ["uv", "run"]:
            return self.mutation()
        return 0, b""


def _is_verify_clean(args: list[str]) -> bool:
    return args[1:2] == [str(SCRIPTS / "run_record.py")] and args[2:5] == [
        "gate",
        "--kind",
        "verify-clean",
    ]


def _write_verify_clean(  # noqa: PLR0913 - the module and repository, then keyword-only fields
    gate: ModuleType,
    repo: Path,
    *,
    tree: str | None = None,
    exit_code: int = 0,
    canonical: bool = True,
    clean: bool = True,
) -> Path:
    """Write a verify-clean gate record and its one pytest-unit child, as make verify-clean does."""
    records = repo / ".runs"
    run = {
        "tree": tree or git(repo, "rev-parse", "HEAD^{tree}"),
        "exit_code": exit_code,
        "canonical": canonical,
        "clean": clean,
        "report_missing": False,
    }
    child = gate.run_record.write_record("pytest-unit", run, tests=[], root=records)
    step: dict[str, Any] = {"name": "verify-clean-run", "exit_code": exit_code, "child_records": []}
    return gate.run_record.write_record(
        "verify-clean",
        {**run, "child_records": [f"pytest-unit/{child.name}"]},
        steps=[step],
        root=records,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a clean fixture repository with a CONTRIBUTING.md that pins ASH."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("ADLYTICS_ENV", "dev")
    monkeypatch.delenv("RUN_RECORD_ROOT", raising=False)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, ".gitignore", ".runs/\n")
    commit(path, "CONTRIBUTING.md", CONTRIBUTING)
    yield path
    _make_writable(path / ".runs")
    _make_writable(tmp_path / "runs")


def _found(name: str) -> str:
    return f"/fake/{name}"


def _only_uvx(name: str) -> str | None:
    return _found(name) if name == "uvx" else None


def _passing_flats() -> dict[str, dict[str, Any]]:
    return {
        "trivy": _flat([], [_metric("trivy-repo"), _metric("bandit", "SKIPPED")]),
        "source": _flat([_finding("HIGH", suppressed=True), _finding("LOW")]),
        "templates": _flat(
            [_finding("MEDIUM", scanner="cfn-nag")],
            [_metric(n) for n in ("cdk-nag", "cfn-nag", "checkov", "detect-secrets")],
        ),
    }


def _gate(  # noqa: PLR0913, PLR0917 - the module, the repository, the tools and the output
    gate: ModuleType, repo: Path, tools: FakeTools, lines: list[str]
) -> int:
    return gate.run_gate(
        repo,
        runner=tools,
        which=_found,
        runs_root=repo.parent / "runs",
        contributing=repo / "CONTRIBUTING.md",
        out=lines.append,
    )


def _record(gate: ModuleType, repo: Path) -> Path:
    records = gate.run_record.record_dirs(repo.parent / "runs", "release-gate")
    assert len(records) == 1
    assert gate.run_record.verify_record(records[0]) == []
    return records[0]


def _raise() -> tuple[int, bytes]:
    raise RuntimeError("the coverage map is unreadable")


@pytest.mark.parametrize(
    "mutation", [lambda: (2, b"mutation: refused\n"), _raise], ids=["exits-2", "raises"]
)
def test_a_failing_mutation_summary_leaves_a_passing_gate_passing(
    gate: ModuleType, repo: Path, mutation: Callable[[], tuple[int, bytes]]
) -> None:
    tools = FakeTools(gate, repo, _passing_flats())
    tools.mutation = mutation
    lines: list[str] = []

    assert _gate(gate, repo, tools, lines) == 0

    assert any("mutation-summary WARNING" in line for line in lines)
    assert lines[-2] == f"release-gate: passed for HEAD {git(repo, 'rev-parse', 'HEAD')[:12]}"
    record = _record(gate, repo)
    run = json.loads((record / "run.json").read_text())
    assert run["outcome"] == "pass"
    assert run["refused"] == []
    assert run["mutation_summary"]["warning"] is not None
    assert {"python", "uv", "git", "platform", "ash", "container_runtime"} <= set(
        run["tool_versions"]
    )
    assert run["ash"]["source"]["by_scanner_severity_state"]["bandit|HIGH|suppressed"] == 1
    assert [c.split("/")[0] for c in run["child_records"]] == ["pytest-unit", "verify-clean"]
    assert run["verify_clean_reused"] is None
    steps = json.loads((record / "steps.json").read_text())
    assert [s["name"] for s in steps][-4:] == [
        "verify-clean",
        "ash",
        "checkout",
        "mutation-summary",
    ]
    assert steps[-1]["report_only"] is True
    for name in ("trivy", "source", "templates"):
        assert (record / "raw" / "ash" / name / "reports" / "ash.flat.json").is_file()
    assert git(repo, "status", "--porcelain") == ""


def test_an_open_high_refuses_whatever_the_mutation_summary_says(
    gate: ModuleType, repo: Path
) -> None:
    flats = _passing_flats()
    flats["templates"]["findings"].append(_finding("HIGH", scanner="checkov"))
    tools = FakeTools(gate, repo, flats)
    lines: list[str] = []

    assert _gate(gate, repo, tools, lines) == 1

    assert (
        "release-gate: ash REFUSED ASH_OPEN_FINDING templates: checkov HIGH B999 src/example.py x1"
        in lines
    )
    run = json.loads((_record(gate, repo) / "run.json").read_text())
    assert run["refused"] == ["ASH_OPEN_FINDING"]
    assert run["mutation_summary"]["warning"] is None
    ash_scans = [call for call in tools.calls if call[0] == "uvx"]
    assert [call[call.index("--output-dir") + 1].rsplit("/", 1)[-1] for call in ash_scans] == [
        "trivy",
        "source",
        "templates",
    ]
    assert all(f"@v{PIN}" in call[2] for call in ash_scans)


def test_a_dirty_tree_or_a_missing_runtime_stops_before_any_stage(
    gate: ModuleType, repo: Path
) -> None:
    (repo / "stray.txt").write_text("untracked\n")
    tools = FakeTools(gate, repo, _passing_flats())
    lines: list[str] = []

    code = gate.run_gate(
        repo,
        runner=tools,
        which=_only_uvx,
        runs_root=repo.parent / "runs",
        contributing=repo / "CONTRIBUTING.md",
        out=lines.append,
    )

    assert code == 1
    assert (
        "release-gate: precondition clean-tree REFUSED DIRTY_TREE "
        "1 uncommitted or untracked path(s)" in lines
    )
    assert (
        "release-gate: precondition tools REFUSED TOOL_MISSING "
        "neither finch nor docker is on PATH" in lines
    )
    assert [call[:2] for call in tools.calls] == [["uv", "run"]]
    (repo / "stray.txt").unlink()


# --------------------------------------------------------------------------------------------
# verify-clean reuse
# --------------------------------------------------------------------------------------------


def test_a_sealed_passing_verify_clean_record_of_head_is_reused(
    gate: ModuleType, repo: Path
) -> None:
    existing = _write_verify_clean(gate, repo)
    name = f"verify-clean/{existing.name}"
    tools = FakeTools(gate, repo, _passing_flats())
    lines: list[str] = []

    assert _gate(gate, repo, tools, lines) == 0

    assert not any(_is_verify_clean(call) for call in tools.calls)
    assert f"release-gate: verify-clean reused {name}" in lines
    record = _record(gate, repo)
    run = json.loads((record / "run.json").read_text())
    assert run["verify_clean_reused"] == name
    assert name in run["child_records"]
    assert any(c.startswith("pytest-unit/") for c in run["child_records"])
    (step,) = [
        s for s in json.loads((record / "steps.json").read_text()) if s["name"] == "verify-clean"
    ]
    assert (step["reused"], step["exit_code"]) == (name, 0)


def _other_tree(gate: ModuleType, repo: Path) -> None:
    _write_verify_clean(gate, repo, tree="cd" * 20)


def _failed(gate: ModuleType, repo: Path) -> None:
    _write_verify_clean(gate, repo, exit_code=2)


def _narrowed(gate: ModuleType, repo: Path) -> None:
    _write_verify_clean(gate, repo, canonical=False)


def _dirty(gate: ModuleType, repo: Path) -> None:
    _write_verify_clean(gate, repo, clean=False)


def _tampered(gate: ModuleType, repo: Path) -> None:
    record = _write_verify_clean(gate, repo)
    _make_writable(record)
    (record / "console.log").write_text("edited\n")


def _child_tampered(gate: ModuleType, repo: Path) -> None:
    _write_verify_clean(gate, repo)
    (child,) = (repo / ".runs" / "pytest-unit").iterdir()
    _make_writable(child)
    (child / "console.log").write_text("edited\n")


def _unsealed(gate: ModuleType, repo: Path) -> None:
    run = {"tree": git(repo, "rev-parse", "HEAD^{tree}"), "canonical": True, "clean": True}
    gate.run_record.LiveRecord.begin("verify-clean", run, root=repo / ".runs", heartbeat=False)


@pytest.mark.parametrize(
    "existing",
    [_other_tree, _failed, _narrowed, _dirty, _tampered, _child_tampered, _unsealed],
    ids=[
        "other-tree",
        "failed",
        "non-canonical",
        "dirty",
        "tampered",
        "child-tampered",
        "unsealed",
    ],
)
def test_verify_clean_runs_when_no_record_can_be_reused(
    gate: ModuleType, repo: Path, existing: Callable[[ModuleType, Path], None]
) -> None:
    existing(gate, repo)
    tools = FakeTools(gate, repo, _passing_flats())
    lines: list[str] = []

    assert _gate(gate, repo, tools, lines) == 0

    assert sum(1 for call in tools.calls if _is_verify_clean(call)) == 1
    assert not any(" reused " in line for line in lines)
    run = json.loads((_record(gate, repo) / "run.json").read_text())
    assert run["verify_clean_reused"] is None


# --------------------------------------------------------------------------------------------
# The scan environment
# --------------------------------------------------------------------------------------------

# Exported in the operator's shell for the test; none may reach ASH, the export or the summary.
# Each gets a neutral value at run time: the names matter here, never a value.
OPERATOR_NAMES = (
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "COSIGN_PASSWORD",
    "DOCKER_CONFIG",
    "SOURCE_DATE_EPOCH",
)


def _export_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for index, name in enumerate(OPERATOR_NAMES):
        monkeypatch.setenv(name, f"operator-value-{index}")


def test_ash_the_export_and_the_summary_run_without_aws_or_forwarded_variables(
    gate: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _export_operator_env(monkeypatch)
    tools = FakeTools(gate, repo, _passing_flats())

    assert _gate(gate, repo, tools, []) == 0

    scans = [
        (call, env)
        for call, env in zip(tools.calls, tools.envs, strict=True)
        if not _is_verify_clean(call)
    ]
    launched = {call[0] for call, _ in scans}
    assert {"uvx", "npm", "make", "npx", "uv"} <= launched
    for call, env in scans:
        leaked = sorted(
            k for k in env if k.startswith(("AWS_", "ADLYTICS_")) or k in gate.CONTAINER_FORWARDED
        )
        assert leaked == [], call
        assert not any(".aws" in arg or "AWS_" in arg for arg in call), call
    for call, _ in scans:
        if call[0] == "uvx":
            # The pinned ASH command and nothing else: no volume, env or credential option.
            assert not {"-v", "--volume", "-e", "--env", "--mount"} & set(call)


def test_the_scan_env_keeps_the_rest_of_the_shell(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _export_operator_env(monkeypatch)
    monkeypatch.setenv("ADLYTICS_ENV", "dev")
    monkeypatch.setenv("FIXTURE_KEPT", "kept")

    env = gate.scan_env()

    assert env["FIXTURE_KEPT"] == "kept"
    assert env["PATH"] == os.environ["PATH"]
    assert not set(OPERATOR_NAMES) & set(env)
    assert "ADLYTICS_ENV" not in env
