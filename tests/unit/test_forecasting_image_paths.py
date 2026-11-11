# SPDX-License-Identifier: MIT-0
"""The job image's closed path list.

A missing module fails the job in SageMaker; an extra one moves the processor-code identity
for code the job never runs.
"""

from __future__ import annotations

import modulefinder
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LIST = SRC / "adapters/jobs/demand_forecast/image-paths.txt"
FIRST_PARTY = ("adapters", "domain", "ports")


def _listed() -> list[str]:
    lines = LIST.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _reached() -> set[str]:
    finder = modulefinder.ModuleFinder(path=[str(SRC)])
    finder.run_script(str(SRC / "adapters/jobs/demand_forecast/entry.py"))
    reached: set[str] = set()
    for name, module in finder.modules.items():
        file: str | None = getattr(module, "__file__", None)
        if name.split(".")[0] in FIRST_PARTY and file:
            reached.add(Path(file).resolve().relative_to(ROOT).as_posix())
    return reached


def test_every_listed_path_exists_once() -> None:
    listed = _listed()
    assert len(listed) == len(set(listed))
    assert [path for path in listed if not (ROOT / path).is_file()] == []


def test_every_reached_module_is_listed_and_every_listed_module_is_reached() -> None:
    entry = "src/adapters/jobs/demand_forecast/entry.py"
    listed_modules = {path for path in _listed() if path.endswith(".py")}
    reached = _reached() | {entry}
    assert sorted(reached - listed_modules) == []
    assert sorted(listed_modules - reached) == []


def test_the_runtime_files_the_image_reads_are_listed() -> None:
    listed = set(_listed())
    assert {
        "contracts/forecasting/demand-forecast.yaml",
        "contracts/generated/demand-forecast-boundaries.json",
        "src/adapters/jobs/demand_forecast/requirements.lock",
        "src/adapters/jobs/demand_forecast/job_log_schema.json",
        "src/adapters/jobs/demand_forecast/entrypoint.sh",
        "src/adapters/jobs/demand_forecast/Dockerfile",
    } <= listed
