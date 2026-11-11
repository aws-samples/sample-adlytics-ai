# SPDX-License-Identifier: MIT-0
"""The mounted-copy artifact roster.

If it walked files differently from the operator command, every run would fail
MODEL_ARTIFACT_MISMATCH.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from adapters.jobs.demand_forecast.artifact_digest import local_roster_entries

pytestmark = pytest.mark.forecasting


def test_entries_are_relative_posix_paths_with_size_and_sha256(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "config.json").write_bytes(b"{}")
    (tmp_path / "nested" / "weights.bin").write_bytes(b"abc")
    entries = local_roster_entries(tmp_path)
    assert [(e.path, e.size) for e in entries] == [("config.json", 2), ("nested/weights.bin", 3)]
    assert entries[1].sha256 == hashlib.sha256(b"abc").hexdigest()


def test_the_artifact_copy_has_config_and_weights(forecast_model_dir: Path) -> None:
    paths = {entry.path for entry in local_roster_entries(forecast_model_dir)}
    assert "config.json" in paths
    assert any(path.endswith(".safetensors") for path in paths)
