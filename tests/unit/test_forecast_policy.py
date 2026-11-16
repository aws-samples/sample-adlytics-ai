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
    assert loaded.section_versions == {
        "input": 1,
        "missing_weeks": 1,
        "eligibility": 1,
        "engine": 1,
        "numeric": 1,
        "gate": 1,
        "serving": 1,
        "retry": 1,
    }


def test_the_committed_gate_and_retry_sections_are_the_approved_values() -> None:
    policy = load_policy(POLICY).policy
    assert policy.gate.metric == "wql"
    assert policy.gate.margin == 0.0
    assert policy.gate.simplicity_order == ("seasonal_naive", "ets", "chronos2")
    assert policy.retry.max_transient_retries == 3
    assert policy.retry.admission_weekday == "thursday"
    assert policy.retry.suppressed_outcomes == (
        "held_quality",
        "failed(target_empty)",
        "failed(job:MODEL_ARTIFACT_MISMATCH)",
        "failed(job:RUN_IDENTITY_MISMATCH)",
        "failed(job:INPUT_SCHEMA_MISMATCH)",
        "failed(job:INPUT_SHARD_MISMATCH)",
        "failed(input_build:MARKETPLACE_UNKNOWN)",
        "failed(input_build:statement_too_large)",
        "failed(input_build:SHARD_PLAN_MISMATCH)",
        "failed(input_build:ROSTER_TOO_LARGE)",
        "rolled_back",
    )


@pytest.mark.parametrize("section", ["gate", "retry"])
def test_a_missing_gate_or_retry_section_is_refused(section: str) -> None:
    document = _document()
    del document[section]
    with pytest.raises(PolicyError, match=rf"policy: missing \['{section}'\], unknown \[\]"):
        parse_policy(document)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("gate", "version"),
        ("gate", "metric"),
        ("gate", "margin"),
        ("gate", "simplicity_order"),
        ("gate", "comparator_fallback"),
        ("gate", "no_valid_simple_model"),
        ("gate", "comparator_quantiles"),
        ("gate", "not_publishable"),
        ("retry", "version"),
        ("retry", "suppressed_outcomes"),
        ("retry", "max_transient_retries"),
        ("retry", "admission_weekday"),
    ],
)
def test_a_missing_gate_or_retry_key_is_refused(section: str, key: str) -> None:
    document = _document()
    del document[section][key]
    with pytest.raises(PolicyError, match=rf"{section}: missing \['{key}'\], unknown \[\]"):
        parse_policy(document)


@pytest.mark.parametrize("section", ["gate", "retry"])
def test_an_unknown_gate_or_retry_key_is_refused(section: str) -> None:
    document = _document()
    document[section]["margn"] = 0.0
    with pytest.raises(PolicyError, match=rf"{section}: missing \[\], unknown \['margn'\]"):
        parse_policy(document)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("gate.metric", "mase", "gate.metric must be wql"),
        ("gate.margin", -0.01, "gate.margin must be at least 0 and below 1"),
        ("gate.margin", 1.0, "gate.margin must be at least 0 and below 1"),
        ("gate.margin", float("nan"), "gate.margin must be at least 0 and below 1"),
        ("gate.margin", True, "gate.margin must be a number"),
        ("gate.margin", "0.0", "gate.margin must be a number"),
        ("gate.simplicity_order", ["ets", "seasonal_naive", "chronos2"], "simplicity_order"),
        ("gate.simplicity_order", ["seasonal_naive", "ets"], "simplicity_order"),
        ("gate.comparator_fallback", "compare_scored", "comparator_fallback must be"),
        ("gate.no_valid_simple_model", "show_untested", "no_valid_simple_model must be"),
        ("gate.comparator_quantiles", "raw", "comparator_quantiles must be"),
        ("gate.not_publishable", "withhold", "not_publishable must be"),
        ("retry.max_transient_retries", -1, "max_transient_retries must be a whole number"),
        ("retry.max_transient_retries", True, "max_transient_retries must be a whole number"),
        ("retry.admission_weekday", "thu", "admission_weekday must be one of"),
        ("retry.admission_weekday", "Thursday", "admission_weekday must be one of"),
        ("retry.suppressed_outcomes", [], "suppressed_outcomes must be non-empty"),
        ("retry.suppressed_outcomes", ["rolled_back"] * 2, "must not repeat"),
        ("retry.suppressed_outcomes", ["failed"], r"failed needs its \(<category>\)"),
        ("retry.suppressed_outcomes", ["published"], "'published' is not held_quality"),
        ("retry.suppressed_outcomes", ["failed(job:)"], r"'failed\(job:\)' is not"),
        ("retry.suppressed_outcomes", ["failed(Job)"], r"'failed\(Job\)' is not"),
    ],
)
def test_a_gate_or_retry_value_outside_its_range_is_refused(
    path: str, value: object, message: str
) -> None:
    section, key = path.split(".")
    document = _document()
    document[section][key] = value
    with pytest.raises(PolicyError, match=message):
        parse_policy(document)


def test_a_positive_margin_and_another_weekday_are_accepted() -> None:
    document = _document()
    document["gate"]["margin"] = 0.05
    document["retry"]["admission_weekday"] = "friday"
    document["retry"]["max_transient_retries"] = 0
    policy = parse_policy(document)
    assert policy.gate.margin == 0.05
    assert policy.retry.admission_weekday == "friday"
    assert policy.retry.max_transient_retries == 0


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
