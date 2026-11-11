# SPDX-License-Identifier: MIT-0
"""The demand-forecast policy loader and validator: an accepted typo would change forecasts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from adapters.jobs.contract_validation.forecast_policy import validate_forecast_policy
from adapters.outbound.demand_forecast_policy import PolicyError, load_policy, parse_policy

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "contracts/forecasting/demand-forecast.yaml"


def _document() -> dict[str, dict[str, object]]:
    return yaml.safe_load(POLICY.read_text())


def test_the_committed_policy_loads_with_its_digest_and_versions() -> None:
    loaded = load_policy(POLICY)
    assert loaded.policy.engine.shard_max_asins == 2000
    assert loaded.policy.eligibility.horizon_weeks == 13
    assert len(loaded.sha256) == 64
    assert set(loaded.section_versions) == {
        "input",
        "missing_weeks",
        "eligibility",
        "engine",
        "numeric",
        "serving",
    }


def test_an_unknown_key_is_refused() -> None:
    document = _document()
    document["engine"]["batch_sise"] = 256
    with pytest.raises(PolicyError, match=r"engine: missing \[\], unknown \['batch_sise'\]"):
        parse_policy(document)


def test_a_shard_size_off_the_ladder_is_refused() -> None:
    document = _document()
    document["engine"]["shard_max_asins"] = 1500
    with pytest.raises(PolicyError, match="shard_max_asins"):
        parse_policy(document)


def test_a_boolean_count_is_refused() -> None:
    document = _document()
    document["eligibility"]["holdout_weeks"] = True
    with pytest.raises(PolicyError, match="holdout_weeks must be a whole number"):
        parse_policy(document)


def test_the_validator_passes_the_repository_and_refuses_a_schema_violation(
    tmp_path: Path,
) -> None:
    assert validate_forecast_policy(ROOT) == ()
    (tmp_path / "contracts/forecasting").mkdir(parents=True)
    shutil.copy(
        ROOT / "contracts/forecasting/demand-forecast.schema.json",
        tmp_path / "contracts/forecasting",
    )
    document = _document()
    del document["serving"]["search"]
    (tmp_path / "contracts/forecasting/demand-forecast.yaml").write_text(yaml.safe_dump(document))
    findings = validate_forecast_policy(tmp_path)
    assert [(f.rule, f.message) for f in findings] == [
        ("forecast_policy_required", "serving.search is required")
    ]
