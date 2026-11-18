# SPDX-License-Identifier: MIT-0
"""Slow-tier fixtures for the demand-forecast job (make check-forecasting, .venv-forecasting).

Tests that need the Chronos-2 artifact read a local copy from `--forecast-model-dir`, defaulting
to the `FORECAST_MODEL_DIR` environment variable, and skip, naming the option, when neither is set.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPORT_MODULE = "run_record_report"
_REPORT_PATH = Path(__file__).resolve().parents[1] / "unit" / f"{_REPORT_MODULE}.py"


def pytest_configure(config: pytest.Config) -> None:
    """Register the run recorder's per-test report, as `tests/unit/conftest.py` does.

    This tier never puts `tests/unit` on the import path, so the plugin is loaded from its file;
    without it `make check-forecasting` seals a record with no report (R1-6).
    """
    module = sys.modules.get(_REPORT_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(_REPORT_MODULE, _REPORT_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load the run-record report plugin from {_REPORT_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_REPORT_MODULE] = module
        spec.loader.exec_module(module)
    module.register(config)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register `--forecast-model-dir`."""
    parser.addoption(
        "--forecast-model-dir",
        default=os.environ.get("FORECAST_MODEL_DIR"),
        help="a local copy of the JumpStart Chronos-2 artifact",
    )


@pytest.fixture(scope="session")
def forecast_model_dir(request: pytest.FixtureRequest) -> Path:
    """Return the local artifact copy, or skip naming how to provide it."""
    value = request.config.getoption("--forecast-model-dir")
    if not value:
        pytest.skip("needs --forecast-model-dir or FORECAST_MODEL_DIR (a local artifact copy)")
    path = Path(str(value))
    if not (path / "config.json").is_file():
        pytest.skip(f"no config.json under --forecast-model-dir {path}")
    return path
