# SPDX-License-Identifier: MIT-0
"""The job's forecast mode on a staged two-kind input (design §4b, §4e).

A job that accepted a renamed column or another shard's rows would publish forecasts of the
wrong series; one that wrote an invalid candidate would stall the publisher.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import gzip
import hashlib
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
    LOCK_PATH,
    POLICY_PATH,
    ForecastJobError,
    ForecastModeRequest,
    run_forecast_mode,
)
from adapters.jobs.demand_forecast.job_log import JobLog
from adapters.jobs.demand_forecast.parquet_input import ParquetForecastInput, arrow_schema
from adapters.outbound.athena.weekly_series import empty_traffic_parquet
from adapters.outbound.demand_forecast_policy import load_policy
from domain.forecasting.artifact_roster import roster_digest
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    InputManifestV1,
    RunReportV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
    boundary_schema_digests,
)
from domain.forecasting.fingerprint import RunIdentity, run_fingerprint
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


IMAGE = "image-asset-hash"


def _identity(artifact_digest: str, image: str = IMAGE) -> dict[str, Any]:
    loaded = load_policy(POLICY_PATH)
    identity = RunIdentity(
        input_fingerprint=HEX,
        policy_sha256=loaded.sha256,
        processor_code_identity=image,
        lock_digest=hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        model_id="amazon/chronos-2",
        model_artifact_digest=artifact_digest,
        boundary_schema_digests=boundary_schema_digests(),
        instance_type="ml.m5.xlarge",
        max_runtime_seconds=3600,
    )
    return {
        "run_fingerprint": run_fingerprint(identity),
        "run_identity": {
            **{k: v for k, v in identity.as_json().items() if k != "execution_plan"},
            "policy_section_versions": loaded.section_versions,
            "instance_type": "ml.m5.xlarge",
            "max_runtime_seconds": 3600,
        },
    }


def _manifest(artifact_digest: str, asin_count: int = 2, image: str = IMAGE) -> dict[str, Any]:
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
        **_identity(artifact_digest, image),
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
        "boundary_schema_digests": boundary_schema_digests(),
    }


def _stage(  # noqa: PLR0913 -- the root and five keyword overrides
    root: Path,
    *,
    rename: str | None = None,
    artifact: str = HEX,
    asin_count: int = 2,
    no_traffic: bool = False,
    manifest: dict[str, Any] | None = None,
) -> Path:
    body = manifest or _manifest(artifact, asin_count)
    InputManifestV1.model_validate(body)
    (root / "input/manifest").mkdir(parents=True)
    (root / "input/manifest/manifest.json").write_text(json.dumps(body))
    _write(root / "input/target/part-0", _target_table(rename))
    traffic = root / "input/traffic/no-traffic-rows.parquet"
    if no_traffic:
        traffic.parent.mkdir(parents=True)
        traffic.write_bytes(empty_traffic_parquet())
    else:
        _write(root / "input/traffic/part-0", _traffic_table())
    return root / "input"


def _request(root: Path, input_dir: Path, model: Path) -> ForecastModeRequest:
    return ForecastModeRequest(
        run_id="run-1",
        shard=0,
        attempt_id="r0a0",
        input_attempt_id="r0a0",
        processor_code_identity=IMAGE,
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
    # The staged series repeat every 13 weeks, so SeasonalNaive's lag-52 holdout is exact (WQL 0)
    # and Chronos-2 can't be strictly lower: the gate shows SeasonalNaive (design §5c).
    assert {r.targets["units_ordered"].chosen_model for r in records} == {"seasonal_naive"}
    assert {"ets" in report.durations_ms, "model" in report.durations_ms} == {True}
    assert {len(r.recent_weeks) for r in records} == {13}
    assert report.asin_count == 2
    assert report.model_artifact_digest == digest


@pytest.mark.parametrize(
    ("change", "component"),
    [
        ({"image": "another-image"}, "processor_code_identity"),
        ({"run_fingerprint": "c" * 64}, "run_fingerprint"),
        ({"policy_sha256": "d" * 64}, "policy_sha256, run_fingerprint"),
    ],
)
def test_a_manifest_naming_other_code_or_policy_fails_run_identity_mismatch_before_the_model(
    tmp_path: Path, change: dict[str, str], component: str
) -> None:
    manifest = _manifest(HEX, image=change.get("image", IMAGE))
    if "run_fingerprint" in change:
        manifest["run_fingerprint"] = change["run_fingerprint"]
    if "policy_sha256" in change:
        manifest["run_identity"]["policy_sha256"] = change["policy_sha256"]
    staged = _stage(tmp_path, manifest=manifest)
    with pytest.raises(ForecastJobError) as refused:
        run_forecast_mode(_request(tmp_path, staged, tmp_path / "no-model"), _log())
    assert str(refused.value) == f"RUN_IDENTITY_MISMATCH: {component}"
    assert not (tmp_path / "output").exists()


def test_the_builders_zero_row_traffic_object_reads_as_no_traffic_rows(tmp_path: Path) -> None:
    shard = ParquetForecastInput(_stage(tmp_path, no_traffic=True)).read_shard(0)
    assert (len(shard.targets), shard.traffic) == (2 * 104 * len(TARGETS), ())


def test_a_shard_with_no_traffic_rows_still_forecasts(
    tmp_path: Path, forecast_model_dir: Path
) -> None:
    digest = roster_digest(local_roster_entries(forecast_model_dir))
    staged = _stage(tmp_path, artifact=digest, no_traffic=True)
    report = run_forecast_mode(_request(tmp_path, staged, forecast_model_dir), _log())
    assert report.counts_by_reason["units_ordered.chosen.seasonal_naive"] == 2


def test_another_artifact_fails_model_artifact_mismatch(
    tmp_path: Path, forecast_model_dir: Path
) -> None:
    staged = _stage(tmp_path, artifact="b" * 64)
    with pytest.raises(ForecastJobError) as refused:
        run_forecast_mode(_request(tmp_path, staged, forecast_model_dir), _log())
    assert refused.value.code == "MODEL_ARTIFACT_MISMATCH"
