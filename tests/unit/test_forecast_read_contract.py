# SPDX-License-Identifier: MIT-0
"""The shared demand forecast wire fixtures and the generated API enums (ADR-163).

The page's decoders are tested over these same fixtures, so a fixture the server could never
send would let the page accept a shape the API refuses. A drifted enum list would let a new
code render as raw text on the page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from domain.forecasting import read_api

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "frontend" / "test" / "fixtures"
MODELS: dict[str, type[BaseModel]] = {
    "list": read_api.DemandForecastListV1,
    "list-empty-filter": read_api.DemandForecastListV1,
    "detail": read_api.DemandForecastDetailV1,
    "detail-not-forecast": read_api.DemandForecastDetailV1,
    "status-stale": read_api.DemandForecastStatusV1,
    "status-no-generation": read_api.DemandForecastStatusV1,
    "refusal": read_api.DemandForecastRefusalV1,
}


@pytest.mark.parametrize("name", sorted(MODELS))
def test_each_wire_fixture_validates_against_its_model(name: str) -> None:
    payload = json.loads((FIXTURES / f"demand-forecast-{name}.json").read_text())
    assert MODELS[name].model_validate(payload).model_dump(mode="json") == payload


def test_the_fixtures_cover_a_money_and_a_unit_display() -> None:
    payload = json.loads((FIXTURES / "demand-forecast-detail.json").read_text())
    targets = payload["targets"]
    assert targets["revenue_shipped"]["p50_totals"]["4"]["display"] == "4938.00"
    assert targets["units_ordered"]["p50_totals"]["4"]["display"] == "42"


def test_the_generated_file_carries_the_domain_enums_and_routes() -> None:
    generated = json.loads(
        (ROOT / "contracts" / "generated" / "demand-forecast-api.json").read_text()
    )
    assert generated["enums"] == {name: list(values) for name, values in read_api.API_ENUMS.items()}
    assert generated["routes"] == ["GET /api/demand-forecast", "GET /api/demand-forecast/status"]
    assert generated["enums"]["FreshnessCause"] == [
        "age_over_limit",
        "last_run_failed",
        "last_run_held",
    ]
    assert generated["enums"]["Hold"] == ["none", "quality"]
