# SPDX-License-Identifier: MIT-0
"""The demand-forecast job lock check: drift, missing hashes and the exact approved set (R4-03)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_forecast_lock", ROOT / "scripts" / "check_forecast_lock.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LOCK = """# header
alpha==1.0 \\
    --hash=sha256:aa \\
    --hash=sha256:bb
beta==2.0 ; sys_platform == 'linux' \\
    --hash=sha256:cc
gamma==3.0 ; sys_platform == 'win32' \\
    --hash=sha256:dd
"""
APPROVED = "alpha==1.0\nbeta==2.0\n"


def test_the_committed_set_passes() -> None:
    module = _module()
    assert module.problems(LOCK, LOCK, APPROVED) == []


def test_a_package_outside_the_approved_set_fails() -> None:
    module = _module()
    lock = LOCK + "pytest==9.0.0 \\\n    --hash=sha256:ee\n"
    assert module.problems(lock, lock, APPROVED) == ["not approved: pytest==9.0.0"]


def test_a_requirement_without_a_hash_fails() -> None:
    module = _module()
    lock = LOCK.replace(" \\\n    --hash=sha256:cc", "")
    assert module.problems(lock, lock, APPROVED) == ["beta==2.0 has no --hash"]


def test_drift_from_a_fresh_export_fails() -> None:
    module = _module()
    found = module.problems(LOCK, LOCK.replace("aa", "ab"), APPROVED)
    assert found == [
        "requirements.lock differs from a fresh export; run make generate-forecast-lock"
    ]


def test_a_missing_approved_package_fails() -> None:
    module = _module()
    assert module.problems(LOCK, LOCK, APPROVED + "delta==4.0\n") == [
        "approved but missing: delta==4.0"
    ]


def test_markers_are_evaluated_for_the_image_platform() -> None:
    module = _module()
    assert module.applicable(module.parse_lock(LOCK)) == {"alpha==1.0", "beta==2.0"}


def test_the_repository_lock_matches_the_approved_list() -> None:
    module = _module()
    lock = (ROOT / "src/adapters/jobs/demand_forecast/requirements.lock").read_text()
    approved = (ROOT / "src/adapters/jobs/demand_forecast/approved-packages.txt").read_text()
    assert module.problems(lock, lock, approved) == []
    assert len(module.approved(approved)) == 53
