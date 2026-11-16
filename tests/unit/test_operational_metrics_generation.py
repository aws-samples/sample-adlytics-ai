# SPDX-License-Identifier: MIT-0
"""The operational-metric projections match the dictionary section and refuse drift (design §9).

Emitters and alarms take their names, units and dimension values only from these projections. A
stale one would let an alarm watch a metric nothing emits, or read the inferred-zero share on the
wrong scale (0.05 against a threshold of 5), so the alarm would never fire.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from adapters.jobs.operational_metrics.__main__ import (
    CONNECTOR_PATH,
    PYTHON_PATH,
    TYPESCRIPT_PATH,
    OperationalMetricProjectionError,
    constant_name,
    main,
    section_digest,
)
from adapters.outbound.metric_dictionary import DICTIONARY_PATH, load_operational_metrics
from domain.operations import generated_operational_metrics as generated

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _connector() -> ModuleType:
    """Load the connector's projection by path: `connectors/` is not on the import path."""
    spec = importlib.util.spec_from_file_location("connector_metrics", ROOT / CONNECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _typescript() -> tuple[str, dict[str, dict[str, object]]]:
    text = (ROOT / TYPESCRIPT_PATH).read_text()
    head, _, body = text.partition("export const OPERATIONAL_METRICS = ")
    assert body.endswith(" as const;\n")
    return head, json.loads(body.removesuffix(" as const;\n"))


def test_the_committed_projections_are_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["--check"]) == 0


def test_the_python_projection_matches_the_section_and_carries_its_digest() -> None:
    declared = load_operational_metrics(ROOT)
    assert [
        (spec.namespace, spec.name, spec.unit, spec.statistic, spec.dimensions)
        for spec in generated.OPERATIONAL_METRICS
    ] == [
        (metric.namespace, metric.name, metric.unit, metric.statistic, metric.dimensions)
        for metric in declared
    ]
    assert section_digest(declared) == generated.SECTION_DIGEST


def test_the_typescript_projection_matches_the_section_and_carries_its_digest() -> None:
    declared = load_operational_metrics(ROOT)
    head, metrics = _typescript()
    assert f'OPERATIONAL_METRICS_DIGEST = "{section_digest(declared)}" as const;' in head
    assert metrics == {
        metric.name: {
            "namespace": metric.namespace,
            "name": metric.name,
            "unit": metric.unit,
            "statistic": metric.statistic,
            "dimensions": {name: list(values) for name, values in metric.dimensions},
        }
        for metric in declared
    }


def test_the_connector_projection_holds_only_the_producer_namespace() -> None:
    connector = _connector()
    assert [spec.name for spec in connector.OPERATIONAL_METRICS] == ["ProducerOutcome"]
    assert connector.PRODUCER_OUTCOME.namespace == "Adlytics/VendorProducer"
    assert connector.PRODUCER_OUTCOME.dimensions == (
        ("class", ("landed", "credentials", "transport", "terminal", "transient")),
    )
    assert connector.SECTION_DIGEST == generated.SECTION_DIGEST


def test_the_inferred_zero_week_share_is_a_percent_in_every_projection() -> None:
    """R7-M3: one scale, 0 to 100, so the alarm's threshold of 5 means 5%."""
    _, metrics = _typescript()
    assert generated.INFERRED_ZERO_WEEK_SHARE.unit == "Percent"
    assert generated.INFERRED_ZERO_WEEK_SHARE.namespace == "Adlytics/DemandForecast"
    assert metrics["InferredZeroWeekShare"]["unit"] == "Percent"


@pytest.fixture
def generated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write the three projections into a root that holds only the dictionary."""
    (tmp_path / DICTIONARY_PATH).parent.mkdir(parents=True)
    shutil.copy(ROOT / DICTIONARY_PATH, tmp_path / DICTIONARY_PATH)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert main(["--check"]) == 0
    return tmp_path


@pytest.mark.parametrize("path", [PYTHON_PATH, TYPESCRIPT_PATH, CONNECTOR_PATH])
def test_an_edited_or_missing_projection_fails_the_check(generated_root: Path, path: Path) -> None:
    target = generated_root / path
    target.write_text(target.read_text().replace('"Percent"', '"None"', 1) + "\n")
    assert main(["--check"]) == 1
    target.unlink()
    assert main(["--check"]) == 1


def test_a_changed_section_makes_every_projection_stale(
    generated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dictionary = generated_root / DICTIONARY_PATH
    text = dictionary.read_text()
    assert "statistic: Maximum" in text
    dictionary.write_text(text.replace("statistic: Maximum", "statistic: Average", 1))
    capsys.readouterr()
    assert main(["--check"]) == 1
    report = capsys.readouterr().err
    assert all(str(path) in report for path in (PYTHON_PATH, TYPESCRIPT_PATH, CONNECTOR_PATH))


def test_a_metric_name_that_is_not_pascal_case_cannot_be_projected() -> None:
    assert constant_name("InferredZeroWeekShare") == "INFERRED_ZERO_WEEK_SHARE"
    with pytest.raises(OperationalMetricProjectionError, match="not a PascalCase name"):
        constant_name("inferred-zero-share")
