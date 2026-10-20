# SPDX-License-Identifier: MIT-0
"""check-publication, the export stage of the publication gate, indexes, proves, then scans.

`scripts/check-publication.sh` is the export stage `make publication-gate BASE=<rev>` runs last
(scripts/publication_gate.py; tests/unit/test_publication_gate.py tests the gate's own range
checks). This is a text test like `test_verify_clean.py`: it proves the order of the commands, not
their behaviour, which `make publication-gate BASE=<rev>` shows when run. Two
orders matter. The export must be indexed before the tracked-file gates, or they skip and pass
vacuously; and the scanner's positive control must run before the export scan, because a scanner
that cannot find a planted key proves nothing by passing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from gate_script_support import SCRIPTS, commit, new_repo, run, script

pytestmark = pytest.mark.unit

SCRIPT = SCRIPTS / "check-publication.sh"
REPOSITORY_ROOT = SCRIPTS.parent
INDEX = ("git init -q", "git add -A")
GATES = (
    "gate P1 make check-publication-boundary",
    "gate P2 make check-links",
    "gate P3 make check-license-headers",
)
CONTROL = 'gitleaks dir "$control"'
EXPORT_SCAN = 'gate P4 gitleaks dir . --redact --no-banner -c "$export_dir/.gitleaks.toml"'
HISTORY_SCAN = 'gate P10a gitleaks git "$root"'
ORDER = ("git archive HEAD | tar -x", 'echo "check-publication: P7 ok', *INDEX, *GATES)
ORDER_REFUSAL = "check-publication must export, check file classes and index before the gates"
CONTROL_REFUSAL = "check-publication must prove the scanner before scanning the export"


def _first(lines: list[str], command: str) -> int | None:
    found = [i for i, line in enumerate(lines) if line.startswith(command)]
    return found[0] if found else None


def _order_findings(text: str) -> list[str]:
    """Return a refusal unless every command appears, in order, at the start of its own line."""
    lines = [line.strip() for line in text.splitlines()]
    positions = [_first(lines, command) for command in ORDER]
    if None in positions or positions != sorted(p for p in positions if p is not None):
        return [ORDER_REFUSAL]
    control, export_scan, history_scan = (
        _first(lines, command) for command in (CONTROL, EXPORT_SCAN, HISTORY_SCAN)
    )
    last_gate = positions[-1]
    if (
        control is None
        or export_scan is None
        or history_scan is None
        or last_gate is None
        or not last_gate < control < export_scan < history_scan
    ):
        return [CONTROL_REFUSAL]
    return []


def test_the_real_script_indexes_proves_the_scanner_then_scans() -> None:
    assert _order_findings(SCRIPT.read_text()) == []


def test_a_script_that_scans_before_the_control_is_refused() -> None:
    lines = SCRIPT.read_text().splitlines()
    scan = next(i for i, line in enumerate(lines) if line.strip().startswith(EXPORT_SCAN))
    moved = lines[:scan] + lines[scan + 1 :]
    control = next(i for i, line in enumerate(moved) if line.strip().startswith(CONTROL))
    reordered = [*moved[:control], lines[scan], *moved[control:]]

    assert _order_findings("\n".join(reordered)) == [CONTROL_REFUSAL]


def test_a_script_without_the_control_is_refused() -> None:
    text = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.strip().startswith(CONTROL)
    )

    assert _order_findings(text) == [CONTROL_REFUSAL]


def test_a_script_without_the_index_step_is_refused() -> None:
    text = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if line.strip() != "git init -q"
    )

    assert _order_findings(text) == [ORDER_REFUSAL]


def test_the_control_requires_the_access_key_rule_and_exit_1() -> None:
    text = SCRIPT.read_text()

    assert "printf 'aws_access_key_id = AKIA%s\\n' \"$token_tail\"" in text
    assert '[ "$control_rc" -ne 1 ]' in text
    assert '"RuleID": *"aws-access-token"' in text
    assert "SCANNER_NOT_PROVEN" in text


def test_the_script_refuses_a_missing_base(tmp_path: Path) -> None:
    result = script("check-publication.sh", cwd=tmp_path)

    assert result.returncode == 2
    assert "usage: check-publication.sh <BASE revision>" in result.stderr


def test_the_make_target_defaults_base_to_the_gate_and_passes_an_explicit_one() -> None:
    make = shutil.which("make")
    assert make is not None
    # Dry runs: make prints the recipe and runs nothing.
    default = run([make, "-n", "--no-print-directory", "publication-gate"], REPOSITORY_ROOT)
    given = run(
        [make, "-n", "--no-print-directory", "publication-gate", "BASE=abc123"], REPOSITORY_ROOT
    )

    assert default.returncode == 0, default.stderr
    assert default.stdout.split() == ["uv", "run", "python", "scripts/publication_gate.py"]
    assert given.stdout.split()[-2:] == ["--base", '"abc123"']
    # The default lives in the gate alone, so the Makefile cannot drift from it.
    gate_source = (SCRIPTS / "publication_gate.py").read_text()
    assert 'DEFAULT_BASE = "public/main"' in gate_source
    assert "default=DEFAULT_BASE" in gate_source


needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="check-publication.sh needs gitleaks on PATH"
)


@pytest.fixture
def fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository whose commits no operator hook or identity decides."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return new_repo(tmp_path)


def _commit_path(repo: Path, path: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    commit(repo, path, "fixture\n")


@needs_gitleaks
@pytest.mark.parametrize(
    "path",
    ["SECURITY-FINDINGS.md", "docs/Security-Findings-2026.md", ".ENV", "frontend/.Env.local"],
)
def test_p7_refuses_a_local_file_class_in_any_letter_case(fixture_repo: Path, path: str) -> None:
    _commit_path(fixture_repo, path)

    result = script("check-publication.sh", "HEAD", cwd=fixture_repo)

    assert result.returncode == 1
    assert "check-publication: P7 FAILED" in result.stderr
    assert path in result.stderr


@needs_gitleaks
@pytest.mark.parametrize("path", [".env.example", "frontend/.ENV.EXAMPLE"])
def test_p7_allows_the_env_example_in_any_letter_case(fixture_repo: Path, path: str) -> None:
    _commit_path(fixture_repo, path)

    result = script("check-publication.sh", "HEAD", cwd=fixture_repo)

    # P7 passes; the fixture has no Makefile, so the next gate, make install, is what stops it.
    assert "check-publication: P7 ok (file classes)" in result.stdout
    assert "P7 FAILED" not in result.stderr
