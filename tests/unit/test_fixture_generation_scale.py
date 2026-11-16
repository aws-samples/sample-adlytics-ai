# SPDX-License-Identifier: MIT-0
"""Scale fixtures for the demand-forecast probes (design §10).

A non-deterministic or memory-growing generator would make the 2,000- and 50,000-ASIN
measurements unrepeatable or unrunnable; a staged input the job refuses would measure nothing.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import io
import tracemalloc
from collections import Counter
from datetime import date

import pyarrow.parquet as pq
import pytest

from adapters.jobs.demand_forecast.parquet_input import arrow_schema
from adapters.jobs.demand_forecast_scale_fixture.__main__ import (
    ScaleStageRequest,
    check_target,
    main,
    newest_complete_saturday,
    stage,
)
from adapters.jobs.fixture_generation.scale import ScaleFixtureRequest, scale_products
from domain.forecasting.boundary import (
    InputManifestV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)

pytestmark = pytest.mark.unit


def _peak(count: int) -> int:
    tracemalloc.start()
    for _ in scale_products(ScaleFixtureRequest(asin_count=count, seed="s", weeks=40)):
        pass
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def test_the_same_seed_gives_the_same_products_and_another_seed_others() -> None:
    first = list(scale_products(ScaleFixtureRequest(asin_count=50, seed="a")))
    assert first == list(scale_products(ScaleFixtureRequest(asin_count=50, seed="a")))
    assert first != list(scale_products(ScaleFixtureRequest(asin_count=50, seed="b")))


def test_memory_stays_flat_as_the_count_grows_fivefold() -> None:
    """The peak at 1,500 products stays within 1.5x the peak at 300: products stream."""
    small, large = _peak(300), _peak(1_500)
    assert large < small * 1.5


def test_every_declared_shape_appears_with_its_declared_absence() -> None:
    products = list(scale_products(ScaleFixtureRequest(asin_count=400, seed="shapes")))
    shapes = Counter(product.shape for product in products)
    assert set(shapes) == {
        "regular",
        "trend_seasonal",
        "low_volume",
        "intermittent",
        "stockout",
        "launch",
        "delisted",
    }
    launch = next(p for p in products if p.shape == "launch")
    assert launch.weeks[:64] == (None,) * 64
    delisted = next(p for p in products if p.shape == "delisted")
    assert delisted.weeks[-20:] == (None,) * 20
    stockout = next(p for p in products if p.shape == "stockout")
    assert any(w is not None and w.units_shipped < w.units_ordered for w in stockout.weeks)


def test_a_staged_input_carries_a_valid_manifest_and_the_jobs_exact_parquet_schemas() -> None:
    staged = stage(
        ScaleStageRequest(
            run_id="scale-3-test",
            bucket="unused",
            fixture=ScaleFixtureRequest(asin_count=3, seed="stage"),
            input_end=date(2026, 10, 3),
            marketplace_key="amazon_us",
            model_artifact_digest="a" * 64,
            instance_type="ml.m5.xlarge",
            max_runtime_seconds=3600,
            processor_code_identity="b" * 64,
        )
    )
    base = "runs/scale-3-test/input/r0a0"
    manifest = InputManifestV1.model_validate_json(staged.objects[f"{base}/manifest.json"])
    assert [s.asin_count for s in manifest.shards] == [3]
    for kind, model in (
        ("target", WeeklyTargetAggregateRowV1),
        ("traffic", WeeklyTrafficAggregateRowV1),
    ):
        part = pq.ParquetFile(io.BytesIO(staged.objects[f"{base}/{kind}/shard=0/part-0"]))
        assert part.schema_arrow.equals(arrow_schema(model), check_metadata=False)
    assert staged.probe["probe"]["shards"] == [{"shard": 0}]  # pyright: ignore[reportIndexIssue]


def test_the_newest_complete_saturday_precedes_today() -> None:
    assert newest_complete_saturday(date(2026, 11, 11)) == date(2026, 11, 7)  # a Wednesday
    assert newest_complete_saturday(date(2026, 11, 7)) == date(2026, 10, 31)  # a Saturday


def test_any_env_but_dev_is_refused_before_any_aws_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--profile=p",
            "--env=prod",
            "--expected-account=" + "1" * 12,
            "--asins=10",
            "--marketplace-key=amazon_us",
            "--model-artifact-digest=x",
            "--instance-type=ml.m5.xlarge",
            "--max-runtime-seconds=3600",
        ]
    )
    assert code == 2
    assert "only against ENV=dev" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env", "expected", "caller", "refusal"),
    [
        ("dev", "1" * 12, "1" * 12, None),
        ("dev", "1" * 12, "2" * 12, "another account than --expected-account"),
        ("dev", "1" * 11, "1" * 11, "must be a 12-digit account ID"),
        ("prod", "1" * 12, "1" * 12, "only against ENV=dev"),
    ],
)
def test_the_probe_runs_only_in_dev_and_only_in_the_expected_account(  # noqa: PLR0913, PLR0917
    env: str, expected: str, caller: str, refusal: str | None
) -> None:
    reads: list[str] = []

    def account() -> str:
        reads.append(caller)
        return caller

    refused = check_target(env, expected, account)
    assert refused is None if refusal is None else refusal in str(refused)
    # The account is read (one STS call) only once the environment and the ID format pass.
    assert reads == ([caller] if env == "dev" and len(expected) == 12 else [])
