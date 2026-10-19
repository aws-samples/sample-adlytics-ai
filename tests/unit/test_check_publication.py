# SPDX-License-Identifier: MIT-0
"""check-publication indexes its export, proves the scanner, then runs every publication gate.

`scripts/check-publication.sh` is a text test like `test_verify_clean.py`: it proves the order of
the commands, not their behaviour, which `make check-publication BASE=<rev>` shows when run. Two
orders matter. The export must be indexed before the tracked-file gates, or they skip and pass
vacuously; and the scanner's positive control must run before the export scan, because a scanner
that cannot find a planted key proves nothing by passing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from gate_script_support import SCRIPTS, run, script

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


def test_the_make_target_refuses_a_missing_base() -> None:
    make = shutil.which("make")
    assert make is not None
    result = run([make, "--no-print-directory", "check-publication"], REPOSITORY_ROOT)

    assert result.returncode != 0
    assert "BASE is required" in result.stdout + result.stderr
