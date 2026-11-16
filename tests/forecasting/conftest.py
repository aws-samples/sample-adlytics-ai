# SPDX-License-Identifier: MIT-0
"""Slow-tier fixtures for the demand-forecast job (make check-forecasting, .venv-forecasting).

Tests that need the Chronos-2 artifact read a local copy from `--forecast-model-dir`, defaulting
to the `FORECAST_MODEL_DIR` environment variable, and skip, naming the option, when neither is set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


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
