# SPDX-License-Identifier: MIT-0
"""The job's forecast mode on a staged two-kind input (design §4b, §4e).

A job that accepted a renamed column or another shard's rows would publish forecasts of the
wrong series; one that wrote an invalid candidate would stall the publisher.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import gzip
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from adapters.jobs.demand_forecast.artifact_digest import local_roster_entries
from adapters.jobs.demand_forecast.entry import (
    ForecastJobError,
    ForecastModeRequest,
    run_forecast_mode,
)
from adapters.jobs.demand_forecast.job_log import JobLog
from adapters.jobs.demand_forecast.parquet_input import arrow_schema
from domain.forecasting.artifact_roster import roster_digest
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    InputManifestV1,
    RunReportV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)
from domain.forecasting.policy import MONEY_TARGETS, TARGETS
from ports.outbound.forecast_input import ForecastInputError

pytestmark = pytest.mark.forecasting

HEX = "a" * 64
INPUT_END = date(2026, 10, 3)
FIRST = INPUT_END - timedelta(days=727)
ASINS = ("amazon_asin:b0test0001", "amazon_asin:b0test0002")


def _write(path: Path, table: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    path.write_bytes(buffer.getvalue())


def _target_table(rename: str | None = None) -> Any:
    rows: list[dict[str, object]] = []
    for n, asin in enumerate(ASINS):
        for week in range(104):
            for target in TARGETS:
                money = target in MONEY_TARGETS
                rows.append(
                    {
                        "schema_version": 1,
                        "canonical_asin": asin,
                        "week_start": FIRST + timedelta(weeks=week),
                        "target": target,
                        "value_sum": Decimal(f"{20 + n * 5 + (week % 13)}.0000"),
                        "days_with_row": 7,
                        "days_with_null": 0,
                        "min_currency": "USD" if money else None,
                        "max_currency": "USD" if money else None,
                    }
                )
    table = pa.Table.from_pylist(rows, schema=arrow_schema(WeeklyTargetAggregateRowV1))
    if rename:
        table = table.rename_columns(
            [rename if c == "value_sum" else c for c in table.column_names]
        )
    return table


def _traffic_table() -> Any:
    rows = [
        {
            "schema_version": 1,
            "canonical_asin": asin,
            "week_start": FIRST + timedelta(weeks=week),
            "glance_views_sum": 500 + week,
            "days_with_traffic_row": 7,
        }
        for asin in ASINS
        for week in range(104)
    ]
    return pa.Table.from_pylist(rows, schema=arrow_schema(WeeklyTrafficAggregateRowV1))


def _manifest(artifact_digest: str, asin_count: int = 2) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "build_attempt_id": "r0a0",
        "set": {
            "marketplace_key": "amazon_us",
            "distributor_view": "MANUFACTURING",
            "selling_program": "RETAIL",
        },
        "input_end": INPUT_END.isoformat(),
        "expected_fence": 0,
        "input_fingerprint": HEX,
        "run_fingerprint": HEX,
        "run_identity": {
            "input_fingerprint": HEX,
            "policy_sha256": HEX,
            "policy_section_versions": {"engine": 1},
            "processor_code_identity": "asset-hash",
            "lock_digest": HEX,
            "model_id": "amazon/chronos-2",
            "model_artifact_digest": artifact_digest,
            "boundary_schema_digests": {"InputManifestV1": HEX},
            "instance_type": "ml.m5.xlarge",
            "max_runtime_seconds": 3600,
        },
        "pins": {"sales": [], "traffic": [], "products": []},
        "shards": [
            {
                "index": 0,
                "asin_count": asin_count,
                "first_canonical_asin": ASINS[0],
                "last_canonical_asin": ASINS[-1],
            }
        ],
        "parquet_objects": [],
        "titles_key": "titles.json",
        "titles_sha256": HEX,
        "coverage": {
            "sales": [{"start": FIRST.isoformat(), "end": INPUT_END.isoformat()}],
            "traffic": [{"start": FIRST.isoformat(), "end": INPUT_END.isoformat()}],
        },
        "newest_publication_published_at": "2026-10-05T00:00:00Z",
        "boundary_schema_digests": {"InputManifestV1": HEX},
    }


def _stage(  # noqa: PLR0913 -- the root and three keyword overrides
    root: Path, *, rename: str | None = None, artifact: str = HEX, asin_count: int = 2
) -> Path:
    InputManifestV1.model_validate(_manifest(artifact, asin_count))
    (root / "input/manifest").mkdir(parents=True)
    (root / "input/manifest/manifest.json").write_text(json.dumps(_manifest(artifact, asin_count)))
    _write(root / "input/target/part-0", _target_table(rename))
    _write(root / "input/traffic/part-0", _traffic_table())
    return root / "input"


def _request(root: Path, input_dir: Path, model: Path) -> ForecastModeRequest:
    return ForecastModeRequest(
        run_id="run-1",
        shard=0,
        attempt_id="r0a0",
        input_attempt_id="r0a0",
        input_dir=input_dir,
        model_dir=model,
        output_dir=root / "output",
    )


def _log() -> JobLog:
    return JobLog("run-1", stream=io.StringIO())


def test_a_renamed_column_fails_input_schema_mismatch_before_the_model(tmp_path: Path) -> None:
    staged = _stage(tmp_path, rename="value_total")
    with pytest.raises(ForecastInputError) as refused:
        run_forecast_mode(_request(tmp_path, staged, tmp_path / "no-model"), _log())
    assert refused.value.code == "INPUT_SCHEMA_MISMATCH"
    assert not (tmp_path / "output").exists()


def test_a_shard_whose_asins_differ_from_its_shardv1_fails_input_shard_mismatch(
    tmp_path: Path,
) -> None:
    staged = _stage(tmp_path, asin_count=3)
    with pytest.raises(ForecastInputError) as refused:
        run_forecast_mode(_request(tmp_path, staged, tmp_path / "no-model"), _log())
    assert refused.value.code == "INPUT_SHARD_MISMATCH"


def test_a_staged_input_gives_valid_candidates_and_a_run_report(
    tmp_path: Path, forecast_model_dir: Path
) -> None:
    digest = roster_digest(local_roster_entries(forecast_model_dir))
    staged = _stage(tmp_path, artifact=digest)
    report = run_forecast_mode(_request(tmp_path, staged, forecast_model_dir), _log())
    RunReportV1.model_validate(json.loads((tmp_path / "output/run-report.json").read_text()))
    lines = gzip.decompress((tmp_path / "output/series.jsonl.gz").read_bytes()).splitlines()
    records = [CandidateSeriesV1.model_validate_json(line) for line in lines]
    assert [r.canonical_asin for r in records] == list(ASINS)
    assert {r.targets["units_ordered"].chosen_model for r in records} == {"chronos2"}
    assert {len(r.recent_weeks) for r in records} == {13}
    assert report.asin_count == 2
    assert report.model_artifact_digest == digest


def test_another_artifact_fails_model_artifact_mismatch(
    tmp_path: Path, forecast_model_dir: Path
) -> None:
    staged = _stage(tmp_path, artifact="b" * 64)
    with pytest.raises(ForecastJobError) as refused:
        run_forecast_mode(_request(tmp_path, staged, forecast_model_dir), _log())
    assert refused.value.code == "MODEL_ARTIFACT_MISMATCH"
