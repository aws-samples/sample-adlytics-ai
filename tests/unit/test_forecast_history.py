# SPDX-License-Identifier: MIT-0
"""The STAGED demand-forecast input built from the synthetic catalogue (slice 1 amendment).

Acceptance 3 to 6 run on this input instead of landed history. An input that drifts from the
input builder's format, covers the unlanded day, or invents rows on a shape's absent days would
prove the engine on data the lake could never produce.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import functools
import io
import pathlib
from datetime import date, timedelta

import pyarrow.parquet as pq
import pytest

from adapters.jobs.demand_forecast.parquet_input import arrow_schema
from adapters.jobs.demand_forecast_scale_fixture.__main__ import (
    ScaleStageRequest,
    StagedInput,
    main,
    stage_catalogue,
)
from adapters.jobs.fixture_generation.catalog import (
    SyntheticEntityCatalog,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.forecast_history import (
    CatalogueHistory,
    CatalogueHistoryRequest,
    canonical_asin,
    catalogue_history,
)
from adapters.jobs.fixture_generation.scale import ScaleFixtureRequest
from domain.forecasting.boundary import (
    InputManifestV1,
    TitleMapV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
INPUT_END = date(2026, 10, 3)
BASE = "runs/staged-catalogue-test/input/r0a0"


@functools.cache
def _catalog() -> SyntheticEntityCatalog:
    return load_synthetic_entity_catalog(ROOT)


@functools.cache
def _history() -> CatalogueHistory:
    return catalogue_history(CatalogueHistoryRequest(_catalog(), INPUT_END))


@functools.cache
def _staged() -> StagedInput:
    request = ScaleStageRequest(
        run_id="staged-catalogue-test",
        bucket="unused",
        fixture=ScaleFixtureRequest(asin_count=1, seed="unused"),
        input_end=INPUT_END,
        marketplace_key="amazon_us",
        model_artifact_digest="a" * 64,
        instance_type="ml.m5.xlarge",
        max_runtime_seconds=3600,
        processor_code_identity="b" * 64,
    )
    return stage_catalogue(request, _history())


def _asins(shape: str) -> list[str]:
    return [canonical_asin(p.asin) for p in _catalog().products if p.forecast_shape == shape]


def test_the_staged_catalogue_is_one_valid_shard_in_the_jobs_exact_parquet_schemas() -> None:
    staged = _staged()
    manifest = InputManifestV1.model_validate_json(staged.objects[f"{BASE}/manifest.json"])
    assert [s.asin_count for s in manifest.shards] == [60]
    assert manifest.input_end == INPUT_END
    for kind, model in (
        ("target", WeeklyTargetAggregateRowV1),
        ("traffic", WeeklyTrafficAggregateRowV1),
    ):
        part = pq.ParquetFile(io.BytesIO(staged.objects[f"{BASE}/{kind}/shard=0/part-0"]))
        assert part.schema_arrow.equals(arrow_schema(model), check_metadata=False)
    titles = TitleMapV1.model_validate_json(staged.objects[f"{BASE}/titles.json"])
    assert {entry.title for entry in titles.entries} == {p.title for p in _catalog().products}


def test_the_unlanded_day_is_uncovered_and_its_week_holds_six_days_for_a_regular_product() -> None:
    unlanded = _catalog().shape.forecast.vendor_day_unlanded
    assert unlanded == date(2025, 6, 17)
    covered = _history().coverage
    assert all(not low <= unlanded <= high for low, high in covered)
    assert covered[0][0] == date(2024, 10, 8)
    assert covered[-1][1] == INPUT_END
    week_start = unlanded - timedelta(days=(unlanded.weekday() + 1) % 7)
    for asin in _asins("regular"):
        (week,) = [w for w in _history().weeks[asin] if w.week_start == week_start]
        assert week.days_with_row == 6, asin


def test_launch_and_delisted_products_have_weeks_only_on_their_declared_side() -> None:
    forecast = _catalog().shape.forecast
    for asin in _asins("launch"):
        weeks = _history().weeks[asin]
        assert weeks
        assert all(w.week_start + timedelta(days=6) >= forecast.launch_first_day for w in weeks)
    for asin in _asins("delisted"):
        weeks = _history().weeks[asin]
        assert weeks
        assert all(w.week_start <= forecast.delisted_last_day for w in weeks)


def test_a_regular_product_has_104_weeks_and_values_in_whole_units_and_cents() -> None:
    for asin in _asins("regular"):
        weeks = _history().weeks[asin]
        assert len(weeks) == 104, asin
        assert all(w.units_shipped == w.units_shipped.to_integral_value() for w in weeks)
        assert all(w.revenue_shipped.as_tuple().exponent >= -4 for w in weeks)  # pyright: ignore[reportOperatorIssue]


def test_an_input_end_that_is_not_a_saturday_is_refused() -> None:
    with pytest.raises(ValueError, match="input end must be a Saturday"):
        CatalogueHistoryRequest(_catalog(), date(2026, 10, 2))
    assert CatalogueHistoryRequest(_catalog(), INPUT_END).weeks == 104


def test_the_catalogue_source_takes_no_asin_count(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--profile", "unused", "--env", "dev", "--expected-account", "1" * 12,
            "--source", "catalogue", "--asins", "5",
            "--marketplace-key", "amazon_us", "--model-artifact-digest", "a" * 64,
            "--instance-type", "ml.m5.xlarge", "--max-runtime-seconds", "3600",
        ]
    )  # fmt: skip
    assert code == 2
    assert "--asins is required with --source scale and only then" in capsys.readouterr().err
