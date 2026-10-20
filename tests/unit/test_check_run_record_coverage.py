# SPDX-License-Identifier: MIT-0
"""Every Makefile test or scan target writes a run record, and the check refuses one that does not.

`scripts/check-run-record-coverage.py` runs in `make check`. A failure here means a test, an
acceptance probe or a scan could run from the Makefile and leave no record, so a claim that it
passed could not be tied to the tree it ran on (docs/test-run-records.md).
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest
from gate_script_support import SCRIPTS

pytestmark = pytest.mark.unit

RECORDED = "\t@uv run python scripts/run_record.py run --kind $@ --format exit -- uv run probe\n"


@pytest.fixture(scope="module")
def coverage() -> ModuleType:
    """Load the hyphenated script as a module so its functions are called directly."""
    spec = importlib.util.spec_from_file_location(
        "check_run_record_coverage", SCRIPTS / "check-run-record-coverage.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_repository_makefile_records_every_test_and_scan_target(coverage: ModuleType) -> None:
    assert coverage.findings(coverage.MAKEFILE.read_text()) == []


@pytest.mark.parametrize(
    "rule",
    [
        "test-fast: ## quick tests\n\t@uv run pytest -m unit tests/unit/test_a.py\n",
        "accept-thing: ## a deployed probe\n\t@uv run python validation/probe_thing.py\n",
        "check-widgets: ## a check that is really a jest run\n\t@cd infra && npx jest test/w.ts\n",
        "scan-secrets: ## a scan under any name\n\t@gitleaks dir .\n",
    ],
    ids=["test-name", "accept-name", "runner-in-recipe", "scanner-in-recipe"],
)
def test_a_target_that_bypasses_the_recorder_is_refused(coverage: ModuleType, rule: str) -> None:
    name = rule.split(":", 1)[0]

    assert coverage.findings(rule, {}) == [
        f"Makefile:1: {name} runs tests or a scan without the run recorder"
    ]


def test_recorded_exempt_and_unrelated_targets_pass(coverage: ModuleType) -> None:
    text = (
        f"accept-thing: ## recorded\n{RECORDED}"
        "report-only: ## names vitest and pytest in its description only\n\t@echo ok\n"
        "regen: ## writes a file\n\t@uv run vulture --make-whitelist > allow.py\n"
        "define helper\n\tuv run pytest\nendef\n"
    )

    assert coverage.findings(text, {"regen": "regenerates a file"}) == []


def test_a_stale_exemption_is_refused(coverage: ModuleType) -> None:
    text = "lint: ## not a test\n\t@uv run ruff check\n"

    assert coverage.findings(text, {"lint": "no longer a test"}) == [
        "EXEMPT names lint, which is not a test or scan target in the Makefile"
    ]
