# SPDX-License-Identifier: MIT-0
"""verify-clean indexes its export before the tracked-file gates, then runs every gate.

`scripts/verify-clean.sh` runs the gates in `git archive HEAD | tar -x`, which has no `.git`.
The publication-boundary and link gates enumerate tracked files with git and skip when there is
no repository, so the export must be indexed before `make check` or gates C, D and E would pass
vacuously. This is a text test: it proves the order of the commands, not their behaviour, which
the operator checks by running `make verify-clean` in a scratch clone.

The export is also judged without the operator's `ADLYTICS_*` deploy flags: the scrub must come
before the first gate, and the scrub block itself is run here against a fictional flag.

The frontend unit tests run through `make frontend-test`, after `make infra-test`, so the clean
export writes a frontend-test run record as well as the pytest-unit and infra-test ones that
`make verify` and `make infra-test` write; a script that ran `npm test` directly would leave no
record for the publication gate to match against the tree (docs/test-run-records.md).
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-clean.sh"
SCRUB_START = "for name in $(compgen -e); do"
SCRUB_GUARD = "test -z \"$(compgen -e | grep '^ADLYTICS_' || true)\""
ORDER = (
    SCRUB_START,
    "git init -q",
    "git add -A",
    "make install",
    "make verify",
    "make check",
    "make infra-test",
    "make frontend-test",
)
REFUSAL = "verify-clean must index the export before make check"
SCRUB_REFUSAL = "verify-clean must unset every ADLYTICS_* variable before the first gate"


def _order_findings(text: str) -> list[str]:
    """Return a refusal unless every required command appears, in order, as its own line.

    A scrub that is missing or not first gets its own refusal; any other gap or misorder gets the
    index refusal.
    """
    lines = [line.strip() for line in text.splitlines()]
    positions: list[int | None] = []
    for command in ORDER:
        found = [
            i for i, line in enumerate(lines) if line == command or line.startswith(command + " ")
        ]
        positions.append(found[0] if found else None)
    scrub, *gates = positions
    present = [position for position in gates if position is not None]
    if scrub is None or any(position < scrub for position in present):
        return [SCRUB_REFUSAL]
    if len(present) < len(gates) or present != sorted(present):
        return [REFUSAL]
    return []


def test_a_script_without_the_index_step_is_refused() -> None:
    text = SCRIPT.read_text()
    without_index = "\n".join(line for line in text.splitlines() if line.strip() != "git init -q")

    assert _order_findings(without_index) == [REFUSAL]


def test_a_script_that_checks_before_indexing_is_refused() -> None:
    reordered = "\n".join(
        (
            SCRUB_START,
            "make check",
            "git init -q",
            "git add -A",
            "make install",
            "make verify",
            "make infra-test",
            "make frontend-test",
        )
    )

    assert _order_findings(reordered) == [REFUSAL]


def test_the_real_script_indexes_then_runs_every_gate() -> None:
    assert _order_findings(SCRIPT.read_text()) == []


def _scrub_block(text: str) -> str:
    """Return the script's lines from the scrub loop through its guard, as written."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == SCRUB_START)
    end = next(i for i, line in enumerate(lines) if line.strip().startswith(SCRUB_GUARD))
    return "\n".join(lines[start : end + 1])


def test_a_script_that_scrubs_after_the_first_gate_is_refused() -> None:
    reordered = "\n".join(
        (
            "git init -q",
            "git add -A",
            "make install",
            SCRUB_START,
            "make verify",
            "make check",
            "make infra-test",
            "make frontend-test",
        )
    )
    assert _order_findings(reordered) == [SCRUB_REFUSAL]


def test_the_scrub_block_removes_every_adlytics_variable() -> None:
    block = _scrub_block(SCRIPT.read_text())
    env = {
        "PATH": os.environ["PATH"],
        "ADLYTICS_ONLINE_EVALUATION_PROVISIONED": "true",
        "ADLYTICS_FICTIONAL_FLAG": "fictional-value",
        "FICTIONAL_KEPT": "kept",
    }
    # A-SUBPROCESS-001: fixed interpreter and code; see docs/security-dispositions.md
    # The interpreter and the script's scrub block are test constants; no caller input reaches it.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"set -euo pipefail\n{block}\nenv"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stderr) == (0, "")
    names = sorted(line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line)
    assert [name for name in names if name.startswith("ADLYTICS_")] == []
    assert "FICTIONAL_KEPT" in names
