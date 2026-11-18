# SPDX-License-Identifier: MIT-0
"""Dev-only demand-forecast scale probe: stage a weekly input for N fictional ASINs, then start it.

Writes, under `runs/<run_id>/input/r0a0/` of the dev processing bucket, both `UNLOAD`-shaped
Parquet kinds partitioned by shard (`target/shard=<k>/`, `traffic/shard=<k>/`), an
`InputManifestV1` with the newest complete Saturday as `input_end` and the set's `CURRENT`
fence as `expected_fence` (0 before the first outcome), and a `TitleMapV1`; then starts the
deployed state machine with the `ProbeStart` input, so the run enters at `Forecast` (design
§10, "Scale fixtures"). The probe's commit is fenced like a real run's: if anything moves
`CURRENT` between staging and commit, the probe ends `superseded`. Refuses every environment
but `dev`, a profile whose account isn't `--expected-account` (checked before any other AWS
call) and an existing run prefix. The manifest's processor-code identity is the deployed input
builder's, so the job's run-identity check holds the staged input to the image that runs it.
Products come from `fixture_generation.scale`, so nothing here is a customer value. Prints the
run ID, the shard count and the execution ARN's name only.

`--source catalogue` stages the synthetic catalogue's 104-week Vendor history instead
(`fixture_generation.forecast_history`), in the same format, under a `staged-catalogue-` run ID.
It is a dev rehearsal input only; slice 1's acceptance reads the landed lake path, not this one.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.fixture_generation.forecast_history import (
    CatalogueHistory,
    CatalogueHistoryRequest,
    CatalogueWeek,
    catalogue_history,
)
from adapters.jobs.fixture_generation.scale import (
    ScaleFixtureRequest,
    ScaleProduct,
    scale_asin,
    scale_products,
)
from adapters.outbound.demand_forecast_policy import load_policy
from adapters.outbound.dynamodb.demand_forecast_store import (
    STORE_CLIENT_CONFIG,
    DynamoDbForecastStore,
    forecast_table_name,
)
from domain.forecasting.boundary import (
    ForecastSetV1,
    InputManifestV1,
    TitleMapV1,
    boundary_schema_digests,
)
from domain.forecasting.fingerprint import RunIdentity, run_fingerprint, run_hash12
from domain.forecasting.identity import display_asin
from domain.forecasting.models import CHRONOS2_MODEL_ID
from domain.forecasting.policy import MONEY_TARGETS, TARGETS
from domain.forecasting.publish import forecast_set_id
from domain.forecasting.shards import ShardPlanRequest, plan_shards

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

    from domain.forecasting.publish import CurrentPointer

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
    processor_code_identity: str
    """The deployed image asset hash (the input builder's `PROCESSOR_CODE_IDENTITY`)."""
    expected_fence: int
    """The set's `CURRENT` fence when staged (§10), so the commit is fenced like a real run's."""


@dataclass(frozen=True)
class StagedInput:
    """The staged objects (key to bytes) and the `ProbeStart` input."""

    objects: dict[str, bytes]
    probe: dict[str, object]


@dataclass(frozen=True)
class _Assembly:
    """One staged input's rows per shard, titles, coverage and identity, before assembly."""

    request: ScaleStageRequest
    asins: tuple[str, ...]
    rows: Callable[[str], tuple[list[dict[str, Any]], list[dict[str, Any]]]]
    """Target and traffic rows of one canonical ASIN."""
    titles: Callable[[int, str], str]
    coverage: tuple[tuple[date, date], ...]
    fixture_identity: dict[str, object]


def _week_starts(input_end: date, weeks: int) -> tuple[date, ...]:
    last = input_end - timedelta(days=6)
    return tuple(last - timedelta(weeks=weeks - 1 - week) for week in range(weeks))


def stage(request: ScaleStageRequest) -> StagedInput:
    """Build every object of the staged scale input in memory, shard by shard."""
    starts = _week_starts(request.input_end, request.fixture.weeks)
    asins = tuple(scale_asin(index) for index in range(request.fixture.asin_count))
    products = scale_products(request.fixture)

    def rows(_asin: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        product = next(products)
        return list(target_rows(product, starts)), list(traffic_rows(product, starts))

    window = (starts[0], request.input_end)
    return _assemble(
        _Assembly(
            request=request,
            asins=asins,
            rows=rows,
            titles=lambda index, _asin: f"Scale product {index}",
            coverage=(window,),
            fixture_identity={
                "seed": request.fixture.seed,
                "asins": len(asins),
                "input_end": str(request.input_end),
            },
        )
    )


def catalogue_target_rows(week: CatalogueWeek) -> Iterator[dict[str, Any]]:
    """Yield one catalogue product-week's four target aggregates, as the `UNLOAD` writes them."""
    for target in TARGETS:
        money = target in MONEY_TARGETS
        yield {
            "schema_version": 1,
            "canonical_asin": week.canonical_asin,
            "week_start": week.week_start,
            "target": target,
            "value_sum": getattr(week, target),
            "days_with_row": week.days_with_row,
            "days_with_null": 0,
            "min_currency": "USD" if money else None,
            "max_currency": "USD" if money else None,
        }


def catalogue_traffic_row(week: CatalogueWeek) -> dict[str, Any]:
    """Return one catalogue product-week's glance views, as the `UNLOAD` writes them."""
    return {
        "schema_version": 1,
        "canonical_asin": week.canonical_asin,
        "week_start": week.week_start,
        "glance_views_sum": week.glance_views,
        "days_with_traffic_row": week.days_with_row,
    }


def stage_catalogue(request: ScaleStageRequest, history: CatalogueHistory) -> StagedInput:
    """Build the STAGED input from the synthetic catalogue's weekly history (K-4)."""

    def rows(asin: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        weeks = history.weeks[asin]
        return (
            [row for week in weeks for row in catalogue_target_rows(week)],
            [catalogue_traffic_row(week) for week in weeks],
        )

    return _assemble(
        _Assembly(
            request=request,
            asins=history.canonical_asins,
            rows=rows,
            titles=lambda _index, asin: history.titles[asin],
            coverage=history.coverage,
            fixture_identity={
                "source": "staged-catalogue",
                "asins": len(history.canonical_asins),
                "input_end": str(request.input_end),
            },
        )
    )


def _assemble(assembly: _Assembly) -> StagedInput:
    request = assembly.request
    loaded = load_policy(POLICY)
    policy = loaded.policy
    base = f"runs/{request.run_id}/input/r0a0"
    asins = assembly.asins
    shards = plan_shards(ShardPlanRequest(asins, policy.engine.shard_max_asins)).shards
    objects: dict[str, bytes] = {}
    parquet: list[dict[str, object]] = []
    cursor = iter(asins)
    for shard in shards:
        targets: list[dict[str, Any]] = []
        traffic: list[dict[str, Any]] = []
        for _ in range(shard.asin_count):
            target, views = assembly.rows(next(cursor))
            targets.extend(target)
            traffic.extend(views)
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
            {"canonical_asin": a, "display_asin": display_asin(a), "title": assembly.titles(i, a)}  # pyright: ignore[reportArgumentType]
            for i, a in enumerate(asins)
        ),
    )
    titles_body = (titles.model_dump_json() + "\n").encode("utf-8")
    objects[f"{base}/titles.json"] = titles_body
    input_fingerprint = hashlib.sha256(
        json.dumps(assembly.fixture_identity, sort_keys=True).encode("utf-8")
    ).hexdigest()
    identity = RunIdentity(
        input_fingerprint=input_fingerprint,
        policy_sha256=loaded.sha256,
        processor_code_identity=request.processor_code_identity,
        lock_digest=hashlib.sha256(LOCK.read_bytes()).hexdigest(),
        model_id=CHRONOS2_MODEL_ID,
        model_artifact_digest=request.model_artifact_digest,
        boundary_schema_digests=boundary_schema_digests(),
        instance_type=request.instance_type,
        max_runtime_seconds=request.max_runtime_seconds,
    )
    coverage = [
        {"start": low.isoformat(), "end": high.isoformat()} for low, high in assembly.coverage
    ]
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
            "expected_fence": request.expected_fence,
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
            "coverage": {"sales": coverage, "traffic": coverage},
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
            "run_fingerprint": manifest.run_fingerprint,
            "expected_fence": manifest.expected_fence,
            "shards": [{"shard": s.index} for s in shards],
        }
    }
    return StagedInput(objects, probe)


class CurrentReader(Protocol):
    """The one store read the probe makes."""

    def get_current(self, set_id: str) -> CurrentPointer | None:
        """Read the set's `CURRENT`, or None before its first outcome."""
        ...


def staged_fence(store: CurrentReader, set_id: str) -> int:
    """Return the set's `CURRENT` fence, or 0 before its first outcome (one GetItem)."""
    current = store.get_current(set_id)
    return 0 if current is None else current.fence


def _deployed_fence(session: boto3.Session, env: str, marketplace_key: str) -> int:
    policy = load_policy(POLICY)
    set_id = forecast_set_id(
        ForecastSetV1(
            marketplace_key=marketplace_key,
            distributor_view=policy.policy.input.distributor_view,
            selling_program=policy.policy.input.selling_program,
        )
    )
    store = DynamoDbForecastStore(
        forecast_table_name(env),
        policy.policy.serving.item_max_bytes,
        session.client("dynamodb", config=STORE_CLIENT_CONFIG),
    )
    return staged_fence(store, set_id)


def _prefix_empty(client: S3Client, bucket: str, prefix: str) -> bool:
    # aws-pagination: MaxKeys=1 asks only whether the prefix holds any object.
    return client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) == 0


def check_target(env: str, expected_account: str, caller_account: Callable[[], str]) -> str | None:
    """Return why the target is refused, or None; the account is read only for ENV=dev."""
    if env != "dev":
        return "refused: the scale probe runs only against ENV=dev"
    if re.fullmatch(r"[0-9]{12}", expected_account) is None:
        return "refused: --expected-account must be a 12-digit account ID"
    if caller_account() != expected_account:
        return "refused: the profile resolves to another account than --expected-account"
    return None


def main(argv: list[str]) -> int:
    """Stage the input and start the probe execution; return the exit code."""
    parser = argparse.ArgumentParser(prog="demand-forecast-scale-probe")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--source", choices=("scale", "catalogue"), default="scale")
    parser.add_argument("--asins", type=int, default=None)
    parser.add_argument("--seed", default="adlytics-demand-forecast-scale-r1")
    parser.add_argument("--marketplace-key", required=True)
    # Default: the deployed input builder's digest, which the stack takes from the committed pin.
    parser.add_argument("--model-artifact-digest", default=None)
    parser.add_argument("--instance-type", required=True)
    parser.add_argument("--max-runtime-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    if (args.source == "scale") != (args.asins is not None):
        print("refused: --asins is required with --source scale and only then", file=sys.stderr)
        return 2
    sessions: list[boto3.Session] = []

    def caller_account() -> str:
        sessions.append(boto3.Session(profile_name=args.profile, region_name=args.region))
        return str(sessions[0].client("sts").get_caller_identity()["Account"])

    account = str(args.expected_account)
    refused = check_target(args.env, account, caller_account)
    if refused is not None:
        print(refused, file=sys.stderr)
        return 2
    session = sessions[0]
    identity = (
        session.client("lambda")
        .get_function_configuration(FunctionName=f"adlytics-{args.env}-DemandForecastInput")
        .get("Environment", {})
        .get("Variables", {})
    )
    processor = identity.get("ADL_DEMAND_FORECAST_PROCESSOR_CODE_IDENTITY", "")
    # Default: the digest the deployed builder took from the committed pin (D7).
    artifact_digest = args.model_artifact_digest or identity.get(
        "ADL_DEMAND_FORECAST_MODEL_ARTIFACT_DIGEST", ""
    )
    if not processor or not artifact_digest:
        print(
            "refused: the deployed input builder names no processor identity or artifact digest",
            file=sys.stderr,
        )
        return 2
    bucket = f"adlytics-{args.env}-demand-forecast-{account}"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    catalog = load_synthetic_entity_catalog(ROOT) if args.source == "catalogue" else None
    if catalog is None:
        run_id = f"scale-{args.asins}-{stamp}"
        input_end = newest_complete_saturday(datetime.now(UTC).date())
    else:
        run_id = f"staged-catalogue-{stamp}"
        # The catalogue's newest complete week: its span ends at fixture_span.last_day.
        input_end = newest_complete_saturday(
            catalog.shape.fixture_span.last_day + timedelta(days=1)
        )
    request = ScaleStageRequest(
        run_id=run_id,
        bucket=bucket,
        fixture=ScaleFixtureRequest(asin_count=args.asins or 1, seed=args.seed),
        input_end=input_end,
        marketplace_key=args.marketplace_key,
        model_artifact_digest=artifact_digest,
        instance_type=args.instance_type,
        max_runtime_seconds=args.max_runtime_seconds,
        processor_code_identity=processor,
        expected_fence=_deployed_fence(session, args.env, args.marketplace_key),
    )
    staged = (
        stage(request)
        if catalog is None
        else stage_catalogue(
            request, catalogue_history(CatalogueHistoryRequest(catalog, input_end))
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
