# SPDX-License-Identifier: MIT-0
"""Closed attribution-model vocabulary loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.outbound.attribution_models import load_attribution_models
from domain.semantics.attribution import AttributionMethod, AttributionModelError

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _write_registry(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "contracts/shared/attribution-models.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(body)
    return tmp_path


def test_committed_registry_resolves_only_the_three_governed_models() -> None:
    registry = load_attribution_models(ROOT)

    assert registry.keys == ("first_touch_v1", "last_touch_v1", "linear_v1")
    first = registry.model("first_touch_v1")
    last = registry.model("last_touch_v1")
    linear = registry.model("linear_v1")
    assert first is not None and first.method is AttributionMethod.EARLIEST
    assert last is not None and last.method is AttributionMethod.LATEST
    assert linear is not None and linear.method is AttributionMethod.LINEAR
    assert registry.model("invented_touch") is None


def test_loader_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    body = (ROOT / "contracts/shared/attribution-models.yaml").read_text()
    root = _write_registry(tmp_path, body.replace("version: 1", "version: 1\nversion: 1", 1))

    with pytest.raises(AttributionModelError, match="duplicate YAML key 'version'"):
        load_attribution_models(root)


def test_loader_rejects_an_unknown_allocation_method(tmp_path: Path) -> None:
    body = (ROOT / "contracts/shared/attribution-models.yaml").read_text()
    root = _write_registry(
        tmp_path,
        body.replace("earliest_qualifying_touch", "invented_allocation", 1),
    )

    with pytest.raises(AttributionModelError, match="unknown method 'invented_allocation'"):
        load_attribution_models(root)
