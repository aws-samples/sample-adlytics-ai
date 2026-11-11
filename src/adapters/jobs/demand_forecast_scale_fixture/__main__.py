# SPDX-License-Identifier: MIT-0
"""Dev-only demand-forecast scale probe: stage a weekly input for N fictional ASINs, then start it.

Writes, under `runs/<run_id>/input/r0a0/` of the dev processing bucket, both `UNLOAD`-shaped
Parquet kinds partitioned by shard (`target/shard=<k>/`, `traffic/shard=<k>/`), an
`InputManifestV1` with the newest complete Saturday as `input_end` and `expected_fence` 0, and a
`TitleMapV1`; then starts the deployed state machine with the `ProbeStart` input, so the run
enters at `Forecast` (design §10, "Scale fixtures"). Refuses every environment but `dev`, and an
existing run prefix. Products come from `fixture_generation.scale`, so nothing here is a
customer value. Prints the run ID, the shard count and the execution ARN's name only.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from adapters.jobs.fixture_generation.scale import (
    ScaleFixtureRequest,
    ScaleProduct,
    scale_asin,
    scale_products,
)
from adapters.outbound.demand_forecast_policy import load_policy
from domain.forecasting.boundary import (
    InputManifestV1,
    TitleMapV1,
    boundary_schema_digests,
)
from domain.forecasting.fingerprint import RunIdentity, run_fingerprint, run_hash12
from domain.forecasting.identity import display_asin
from domain.forecasting.models import CHRONOS2_MODEL_ID
from domain.forecasting.policy import MONEY_TARGETS, TARGETS
from domain.forecasting.shards import ShardPlanRequest, plan_shards

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

ROOT = Path(__file__).resolve().parents[4]
POLICY = ROOT / "contracts/forecasting/demand-forecast.yaml"
LOCK = ROOT / "src/adapters/jobs/demand_forecast/requirements.lock"
_TARGET_TYPES = (
    ("schema_version", pa.int8()),
    ("canonical_asin", pa.string()),
    ("week_start", pa.date32()),
    ("target", pa.string()),
    ("value_sum", pa.decimal128(38, 4)),
    ("days_with_row", pa.int8()),
    ("days_with_null", pa.int8()),
    ("min_currency", pa.string()),
    ("max_currency", pa.string()),
)
_TRAFFIC_TYPES = (
    ("schema_version", pa.int8()),
    ("canonical_asin", pa.string()),
    ("week_start", pa.date32()),
    ("glance_views_sum", pa.int64()),
    ("days_with_traffic_row", pa.int8()),
)
TARGET_SCHEMA = pa.schema([pa.field(name, kind) for name, kind in _TARGET_TYPES])
TRAFFIC_SCHEMA = pa.schema([pa.field(name, kind) for name, kind in _TRAFFIC_TYPES])


class ScaleProbeError(Exception):
    """A refused target or input; nothing was written."""


def newest_complete_saturday(today: date) -> date:
    """Return the newest Saturday strictly before `today`, whose week is complete."""
    return today - timedelta(days=(today.weekday() - 5) % 7 or 7)


def _value(product: ScaleProduct, week: int, target: str) -> object:
    cell = product.weeks[week]
    if cell is None:
        return None
    return getattr(cell, target)


def target_rows(product: ScaleProduct, starts: tuple[date, ...]) -> Iterator[dict[str, Any]]:
    """Yield the product's weekly target aggregates, one per week with a row and target."""
    for week, start in enumerate(starts):
        if product.weeks[week] is None:
            continue
        for target in TARGETS:
            money = target in MONEY_TARGETS
            yield {
                "schema_version": 1,
                "canonical_asin": product.canonical_asin,
                "week_start": start,
                "target": target,
                "value_sum": _value(product, week, target),
                "days_with_row": 7,
                "days_with_null": 0,
                "min_currency": "USD" if money else None,
                "max_currency": "USD" if money else None,
            }


def traffic_rows(product: ScaleProduct, starts: tuple[date, ...]) -> Iterator[dict[str, Any]]:
    """Yield the product's weekly glance views, one per week with a row."""
    for week, start in enumerate(starts):
        cell = product.weeks[week]
        if cell is not None:
            yield {
                "schema_version": 1,
                "canonical_asin": product.canonical_asin,
                "week_start": start,
                "glance_views_sum": cell.glance_views,
                "days_with_traffic_row": 7,
            }


def parquet_bytes(rows: list[dict[str, Any]], schema: Any) -> bytes:
    """Return one Parquet object of `rows` in `schema`."""
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), buffer)
    return buffer.getvalue()


@dataclass(frozen=True)
class ScaleStageRequest:
    """What to stage, where, and the run identity the job checks."""

    run_id: str
    bucket: str
    fixture: ScaleFixtureRequest
    input_end: date
    marketplace_key: str
    model_artifact_digest: str
    instance_type: str
    max_runtime_seconds: int


@dataclass(frozen=True)
class StagedInput:
    """The staged objects (key to bytes) and the `ProbeStart` input."""

    objects: dict[str, bytes]
    probe: dict[str, object]


def stage(request: ScaleStageRequest) -> StagedInput:
    """Build every object of the staged input in memory, shard by shard."""
    loaded = load_policy(POLICY)
    policy = loaded.policy
    weeks = request.fixture.weeks
    last = request.input_end - timedelta(days=6)
    starts = tuple(last - timedelta(weeks=weeks - 1 - week) for week in range(weeks))
    base = f"runs/{request.run_id}/input/r0a0"
    asins = tuple(scale_asin(index) for index in range(request.fixture.asin_count))
    shards = plan_shards(ShardPlanRequest(asins, policy.engine.shard_max_asins)).shards
    objects: dict[str, bytes] = {}
    parquet: list[dict[str, object]] = []
    products = scale_products(request.fixture)
    for shard in shards:
        targets: list[dict[str, Any]] = []
        traffic: list[dict[str, Any]] = []
        for _ in range(shard.asin_count):
            product = next(products)
            targets.extend(target_rows(product, starts))
            traffic.extend(traffic_rows(product, starts))
        for kind, rows, schema in (
            ("target", targets, TARGET_SCHEMA),
            ("traffic", traffic, TRAFFIC_SCHEMA),
        ):
            key = f"{base}/{kind}/shard={shard.index}/part-0"
            body = parquet_bytes(rows, schema)
            objects[key] = body
            parquet.append(
                {
                    "key": key,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "rows": len(rows),
                    "kind": kind,
                    "shard": shard.index,
                }
            )
    titles = TitleMapV1(
        schema_version=1,
        entries=tuple(
            {"canonical_asin": a, "display_asin": display_asin(a), "title": f"Scale product {i}"}  # pyright: ignore[reportArgumentType]
            for i, a in enumerate(asins)
        ),
    )
    titles_body = (titles.model_dump_json() + "\n").encode("utf-8")
    objects[f"{base}/titles.json"] = titles_body
    fixture_identity = json.dumps(
        {"seed": request.fixture.seed, "asins": len(asins), "input_end": str(request.input_end)},
        sort_keys=True,
    )
    input_fingerprint = hashlib.sha256(fixture_identity.encode("utf-8")).hexdigest()
    identity = RunIdentity(
        input_fingerprint=input_fingerprint,
        policy_sha256=loaded.sha256,
        processor_code_identity=f"scale-probe:{request.fixture.seed}",
        lock_digest=hashlib.sha256(LOCK.read_bytes()).hexdigest(),
        model_id=CHRONOS2_MODEL_ID,
        model_artifact_digest=request.model_artifact_digest,
        boundary_schema_digests=boundary_schema_digests(),
        instance_type=request.instance_type,
        max_runtime_seconds=request.max_runtime_seconds,
    )
    window = {"start": starts[0].isoformat(), "end": request.input_end.isoformat()}
    manifest = InputManifestV1.model_validate(
        {
            "schema_version": 1,
            "run_id": request.run_id,
            "build_attempt_id": "r0a0",
            "set": {
                "marketplace_key": request.marketplace_key,
                "distributor_view": policy.input.distributor_view,
                "selling_program": policy.input.selling_program,
            },
            "input_end": request.input_end.isoformat(),
            "expected_fence": 0,
            "input_fingerprint": input_fingerprint,
            "run_fingerprint": run_fingerprint(identity),
            "run_identity": {
                **{k: v for k, v in identity.as_json().items() if k != "execution_plan"},
                "policy_section_versions": loaded.section_versions,
                "instance_type": request.instance_type,
                "max_runtime_seconds": request.max_runtime_seconds,
            },
            "pins": {"sales": [], "traffic": [], "products": []},
            "shards": [
                {
                    "index": s.index,
                    "asin_count": s.asin_count,
                    "first_canonical_asin": s.first_canonical_asin,
                    "last_canonical_asin": s.last_canonical_asin,
                }
                for s in shards
            ],
            "parquet_objects": parquet,
            "titles_key": f"{base}/titles.json",
            "titles_sha256": hashlib.sha256(titles_body).hexdigest(),
            "coverage": {"sales": [window], "traffic": [window]},
            "newest_publication_published_at": datetime(
                request.input_end.year, request.input_end.month, request.input_end.day, tzinfo=UTC
            ).isoformat(),
            "boundary_schema_digests": boundary_schema_digests(),
        }
    )
    manifest_key = f"{base}/manifest.json"
    objects[manifest_key] = (manifest.model_dump_json(by_alias=True) + "\n").encode("utf-8")
    probe: dict[str, object] = {
        "probe": {
            "run_id": request.run_id,
            "manifest_key": manifest_key,
            "build_attempt_id": "r0a0",
            "run_hash12": run_hash12(request.run_id),
            "shards": [{"shard": s.index} for s in shards],
        }
    }
    return StagedInput(objects, probe)


def _prefix_empty(client: S3Client, bucket: str, prefix: str) -> bool:
    # aws-pagination: MaxKeys=1 asks only whether the prefix holds any object.
    return client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) == 0


def main(argv: list[str]) -> int:
    """Stage the input and start the probe execution; return the exit code."""
    parser = argparse.ArgumentParser(prog="demand-forecast-scale-probe")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--asins", type=int, required=True)
    parser.add_argument("--seed", default="adlytics-demand-forecast-scale-r1")
    parser.add_argument("--marketplace-key", required=True)
    parser.add_argument("--model-artifact-digest", required=True)
    parser.add_argument("--instance-type", required=True)
    parser.add_argument("--max-runtime-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    if args.env != "dev":
        print("refused: the scale probe runs only against ENV=dev", file=sys.stderr)
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = f"adlytics-{args.env}-demand-forecast-{account}"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"scale-{args.asins}-{stamp}"
    staged = stage(
        ScaleStageRequest(
            run_id=run_id,
            bucket=bucket,
            fixture=ScaleFixtureRequest(asin_count=args.asins, seed=args.seed),
            input_end=newest_complete_saturday(datetime.now(UTC).date()),
            marketplace_key=args.marketplace_key,
            model_artifact_digest=args.model_artifact_digest,
            instance_type=args.instance_type,
            max_runtime_seconds=args.max_runtime_seconds,
        )
    )
    s3 = session.client("s3")
    if not _prefix_empty(s3, bucket, f"runs/{run_id}/"):
        print("refused: the run prefix already holds objects", file=sys.stderr)
        return 2
    for key, body in staged.objects.items():
        s3.put_object(Bucket=bucket, Key=key, Body=body)
    region = session.region_name
    machine = f"arn:aws:states:{region}:{account}:stateMachine:adlytics-dev-demand-forecast"
    started = session.client("stepfunctions").start_execution(
        stateMachineArn=machine, name=run_id, input=json.dumps(staged.probe)
    )
    name = started["executionArn"].rsplit(":", 1)[-1]
    shards = len(staged.probe["probe"]["shards"])  # pyright: ignore[reportIndexIssue, reportArgumentType]
    print(f"run_id={run_id} shards={shards} execution={name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
