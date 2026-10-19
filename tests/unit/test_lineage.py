# SPDX-License-Identifier: MIT-0
"""Metric parity checks in the source-to-canonical lineage gate."""

import importlib.util
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def lineage_module() -> Generator[ModuleType]:
    """Load the hyphenated checker script without making its global findings persist."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "check-lineage.py"
    spec = importlib.util.spec_from_file_location("check_lineage_for_test", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.findings.clear()
    yield module
    module.findings.clear()


def test_typed_deferred_metric_ids_match_documented_ids(lineage_module: ModuleType) -> None:
    deferred = [
        {
            "metric_id": "deferred_metric",
            "label": "Deferred metric",
            "kind": "simple",
            "unit": "currency",
        }
    ]

    lineage_module.check_metric_parity({"deferred_metric"}, {"metrics": {}, "deferred": deferred})

    assert lineage_module.findings == []


@pytest.mark.parametrize(
    ("deferred", "expected_message"),
    [
        ("not-a-list", "must be a list"),
        (["not-a-mapping"], "must be a mapping"),
        ([1], "must be a mapping"),
        (
            [
                {
                    "label": "Deferred metric",
                    "kind": "simple",
                    "unit": "currency",
                }
            ],
            "must contain metric_id",
        ),
        (
            [
                {
                    "metric_id": "deferred_metric",
                    "label": "Deferred metric",
                    "kind": "simple",
                    "unit": "currency",
                    "unexpected": "field",
                }
            ],
            "must contain metric_id",
        ),
        (
            [
                {
                    "metric_id": 1,
                    "label": "Deferred metric",
                    "kind": "simple",
                    "unit": "currency",
                }
            ],
            "non-string field(s): metric_id",
        ),
        (
            [
                {
                    "metric_id": " ",
                    "label": "Deferred metric",
                    "kind": "simple",
                    "unit": "currency",
                }
            ],
            "blank metric_id or label",
        ),
        (
            [
                {
                    "metric_id": "deferred_metric",
                    "label": "\t",
                    "kind": "simple",
                    "unit": "currency",
                }
            ],
            "blank metric_id or label",
        ),
    ],
)
def test_invalid_deferred_entries_produce_controlled_metric_parity_findings(
    lineage_module: ModuleType, deferred: object, expected_message: str
) -> None:
    lineage_module.check_metric_parity(set(), {"metrics": {}, "deferred": deferred})

    assert len(lineage_module.findings) == 1
    category, message = lineage_module.findings[0]
    assert category == "metric-parity"
    assert expected_message in message


def test_real_metric_parity_mismatches_remain_reported(lineage_module: ModuleType) -> None:
    lineage_module.check_metric_parity(
        {"documented_metric"},
        {"metrics": {"dictionary_metric": {}}, "deferred": []},
    )

    assert lineage_module.findings == [
        (
            "metric-parity",
            "in canonical-model.md section 5 but not the dictionary: documented_metric. "
            "The dictionary is the machine-readable form; a doc-only metric cannot be computed.",
        ),
        (
            "metric-parity",
            "in the dictionary but not canonical-model.md section 5: dictionary_metric. "
            "A metric nobody documented is a metric nobody reviewed.",
        ),
    ]


def test_contract_discovery_excludes_observability_registry(
    lineage_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts = tmp_path / "contracts"
    data_contract = contracts / "amc" / "data.yaml"
    observability_contract = contracts / "observability" / "spans.yaml"
    data_contract.parent.mkdir(parents=True)
    observability_contract.parent.mkdir(parents=True)
    data_contract.write_text("target_table: fct_sample\n")
    observability_contract.write_text("version: 1\nspans: []\n")
    monkeypatch.setattr(lineage_module, "ROOT", tmp_path)
    monkeypatch.setattr(lineage_module, "CONTRACTS", contracts)

    assert lineage_module.contracts() == [
        (Path("contracts/amc/data.yaml"), {"target_table": "fct_sample"})
    ]


def test_contract_discovery_excludes_the_alert_rules_but_not_a_data_contract(
    lineage_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts = tmp_path / "contracts"
    data_contract = contracts / "amc" / "data.yaml"
    alert_rules = contracts / "alerts" / "alert-rules.yaml"
    data_contract.parent.mkdir(parents=True)
    alert_rules.parent.mkdir(parents=True)
    data_contract.write_text("target_table: fct_sample\n")
    alert_rules.write_text("version: 1\nrules: []\n")
    monkeypatch.setattr(lineage_module, "ROOT", tmp_path)
    monkeypatch.setattr(lineage_module, "CONTRACTS", contracts)

    # Paired: the data contract beside it is still read.
    assert lineage_module.contracts() == [
        (Path("contracts/amc/data.yaml"), {"target_table": "fct_sample"})
    ]
