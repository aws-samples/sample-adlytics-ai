# SPDX-License-Identifier: MIT-0
"""The demand-forecast input builder: `select_input()` and `build_input()` (design §3c, §6d).

One Lambda (`ForecastInputFunction`) serves both Step Functions states.

- **select** checks the marketplace key against the commerce identity bridge
  (`MARKETPLACE_UNKNOWN`), reads the CURRENT pins of the 1,000 days ending today, picks
  `input_end`, holds `held_input_not_ready` (`no_complete_week`, `input_end_too_old`) and returns
  the input and run fingerprints. Slice 1 has no store, so there is no CURRENT to no-op against.
- **build** re-reads the selection and refuses `SELECTION_MOVED` if its input fingerprint moved;
  reads the roster and cross-checks it against `plan_shards()` (`SHARD_PLAN_MISMATCH`,
  `ROSTER_TOO_LARGE`); reads the forecast marketplace's product titles (`SELECTION_MOVED` when
  the hierarchy's publications differ from the product pins; all-null titles when there is no
  hierarchy); runs the two partitioned UNLOADs into the attempt's fresh prefix
  `runs/<run_id>/input/r<RedriveCount>a<RetryCount>/`; and writes `titles.json` (`TitleMapV1`) and
  `manifest.json` (`InputManifestV1`, `expected_fence` 0), both validated before they are written.

A refusal raises `InputBuildFailedError` whose message is the failure category, so the state machine
and an operator read the same code.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import (
    DemandForecastInputSettings,
    demand_forecast_input_settings,
    tracing_settings,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher.retail_source import (
    CanonicalRetailProductHierarchy,
    RetailProductHierarchySource,
    RetailProductIdentity,
)
from adapters.outbound.athena.weekly_series import (
    AthenaWeeklySeriesSource,
    AthenaWeeklySeriesTarget,
    parquet_row_count,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.demand_forecast_policy import LoadedPolicy, load_policy
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.publication_selection import ForecastHistorySelector
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from adapters.outbound.telemetry.tracing import (
    DEMAND_FORECAST_BUILD,
    DEMAND_FORECAST_SELECT,
    span,
    trace_boundary,
)
from domain.forecasting.boundary import (
    InputManifestV1,
    TitleMapV1,
    boundary_schema_digests,
)
from domain.forecasting.fingerprint import RunIdentity, run_fingerprint, run_hash12
from domain.forecasting.identity import ProductKeyError, display_map
from domain.forecasting.models import CHRONOS2_MODEL_ID
from domain.forecasting.selection import (
    SELECTION_DAYS,
    SelectionWindowRequest,
    WindowPin,
    WindowPins,
    window_pins,
)
from domain.forecasting.shards import RosterTooLargeError, ShardPlanRequest, plan_shards
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.shared_kernel.values import DateRange
from ports.outbound.forecast_history_selection import (
    ForecastHistorySelectionPort,
    ForecastHistorySelectionRequest,
    ForecastHistorySelectionResult,
    PinnedContractHistory,
)
from ports.outbound.weekly_series_source import (
    StatementStat,
    WeeklySeriesError,
    WeeklySeriesPlan,
    WeeklySeriesSourcePort,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

_TRACING = configure_tracing(tracing_settings())
_LOGGER = Logger(service="adlytics-demand-forecast-input")
_RUN_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ATTEMPT: Final = re.compile(r"^r[0-9]{1,4}a[0-9]{1,4}$")
_SHARD_PATH: Final = re.compile(r"/shard=([0-9]{1,3})/")
_TAIL_BYTES: Final = 64 * 1024
_CHUNK: Final = 1024 * 1024


class InputBuildFailedError(Exception):
    """A refusal; the message is the failure category the state machine and operator read."""


def _root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "contracts").is_dir():
            return candidate
    raise InputBuildFailedError("CONTRACTS_UNAVAILABLE")


# ---- composition seams (fakes in unit tests) ----


class ObjectStore(Protocol):
    """The processing bucket operations the builder needs."""

    def list_sizes(self, prefix: str) -> dict[str, int]:
        """Return every key under the prefix with its size."""
        ...

    def sha256(self, key: str) -> str:
        """Stream one object through SHA-256."""
        ...

    def tail(self, key: str, size: int) -> bytes:
        """Return the last `size` bytes of an object."""
        ...

    def put(self, key: str, body: bytes) -> None:
        """Write one object."""
        ...


@dataclass(frozen=True)
class InputBuilder:
    """Everything one invocation reads: settings, policy, bridge and its ports."""

    settings: DemandForecastInputSettings
    policy: LoadedPolicy
    bridge: CommerceIdentityBridge
    selection: ForecastHistorySelectionPort
    titles: RetailProductHierarchySource
    weekly: Callable[[Callable[[], float]], WeeklySeriesSourcePort]
    objects: ObjectStore
    today: Callable[[], date]


@dataclass(frozen=True)
class BuildRequest:
    """One build invocation: the run, its attempt and the selection's input fingerprint."""

    run_id: str
    redrive_count: int
    retry_count: int
    input_fingerprint: str

    @property
    def build_attempt_id(self) -> str:
        """`r<RedriveCount>a<RetryCount>`: a redrive keeps the run ID but gets a fresh prefix."""
        return f"r{self.redrive_count}a{self.retry_count}"


@dataclass(frozen=True)
class _Selected:
    result: ForecastHistorySelectionResult
    pins: WindowPins
    window: DateRange


def _window_pins(history: PinnedContractHistory) -> tuple[WindowPin, ...]:
    return tuple(
        WindowPin(pin.partition, pin.publication_id, pin.fencing_token, pin.digest, bucket)
        for pin, bucket in zip(history.pins, history.buckets, strict=True)
    )


def _select(builder: InputBuilder) -> _Selected:
    key = builder.settings.marketplace_key
    if key not in {mapping.target.value for mapping in builder.bridge.marketplace_mappings}:
        raise InputBuildFailedError("MARKETPLACE_UNKNOWN")
    today = builder.today()
    window = DateRange(today - timedelta(days=SELECTION_DAYS - 1), today)
    policy = builder.policy.policy.input
    selectors = tuple(
        sorted(
            (
                ("distributor_view", policy.distributor_view),
                ("selling_program", policy.selling_program),
            )
        )
    )
    result = builder.selection.select(ForecastHistorySelectionRequest(key, selectors, window))
    pins = window_pins(
        SelectionWindowRequest(
            sales=_window_pins(result.sales),
            traffic=_window_pins(result.traffic),
            covered_sales=result.sales.covered,
            covered_traffic=result.traffic.covered,
            selection_window=window,
        )
    )
    return _Selected(result, pins, window)


def _identity(builder: InputBuilder, input_fingerprint: str) -> RunIdentity:
    settings = builder.settings
    return RunIdentity(
        input_fingerprint=input_fingerprint,
        policy_sha256=builder.policy.sha256,
        processor_code_identity=settings.processor_code_identity,
        lock_digest=settings.lock_digest,
        model_id=CHRONOS2_MODEL_ID,
        model_artifact_digest=settings.model_artifact_digest,
        boundary_schema_digests=boundary_schema_digests(),
        instance_type=settings.instance_type,
        max_runtime_seconds=settings.max_runtime_seconds,
    )


def select_input(builder: InputBuilder) -> dict[str, object]:
    """Return `run` with the fingerprints, or `held_input_not_ready` with its category."""
    selected = _select(builder)
    pins = selected.pins
    if pins.held is not None or pins.input_fingerprint is None or pins.input_end is None:
        return {
            "outcome": "held_input_not_ready",
            "category": None if pins.held is None else pins.held.value,
            "input_end": None if pins.input_end is None else pins.input_end.isoformat(),
        }
    identity = _identity(builder, pins.input_fingerprint)
    return {
        "outcome": "run",
        "category": None,
        "input_end": pins.input_end.isoformat(),
        "input_fingerprint": pins.input_fingerprint,
        "run_fingerprint": run_fingerprint(identity),
        "sales_pins": len(pins.sales),
        "traffic_pins": len(pins.traffic),
    }


def _titles(builder: InputBuilder, selected: _Selected, roster: tuple[str, ...]) -> TitleMapV1:
    hierarchy = builder.titles.hierarchy(selected.window)
    pinned = {pin.publication_id for pin in selected.result.products.pins}
    if hierarchy is not None and set(hierarchy.publication_identities) != pinned:
        raise InputBuildFailedError("SELECTION_MOVED")
    by_asin: Mapping[str, RetailProductIdentity] = {} if hierarchy is None else hierarchy.by_asin
    try:
        shown = display_map(roster).by_canonical
    except ProductKeyError as error:
        raise InputBuildFailedError(error.reason.value) from None
    entries: list[dict[str, object]] = []
    for canonical in roster:
        identity = by_asin.get(canonical)
        entries.append(
            {
                "canonical_asin": canonical,
                "display_asin": shown[canonical],
                "title": None if identity is None else identity.title,
            }
        )
    return TitleMapV1.model_validate({"schema_version": 1, "entries": entries})


def _check_roster(roster: tuple[str, ...], shards: tuple[int, ...], size: int) -> None:
    try:
        plan = plan_shards(ShardPlanRequest(roster, size))
    except RosterTooLargeError:
        raise InputBuildFailedError("ROSTER_TOO_LARGE") from None
    except ValueError:
        raise InputBuildFailedError("SHARD_PLAN_MISMATCH") from None
    expected = tuple(position // size for position in range(len(roster)))
    if shards != expected or sum(shard.asin_count for shard in plan.shards) != len(roster):
        raise InputBuildFailedError("SHARD_PLAN_MISMATCH")


def _rows(objects: ObjectStore, key: str, size: int) -> int:
    tail = objects.tail(key, min(size, _TAIL_BYTES))
    footer = int.from_bytes(tail[-8:-4], "little") + 8
    if footer > len(tail) and footer <= size:
        tail = objects.tail(key, footer)
    return parquet_row_count(tail)


def _objects(builder: InputBuilder, prefix: str, kind: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for key, size in sorted(builder.objects.list_sizes(prefix).items()):
        match = _SHARD_PATH.search(key)
        if match is None or size == 0:
            continue
        found.append(
            {
                "key": key,
                "sha256": builder.objects.sha256(key),
                "rows": _rows(builder.objects, key, size),
                "kind": kind,
                "shard": int(match.group(1)),
            }
        )
    return found


def _pin_json(
    history: PinnedContractHistory, keep: set[str] | None = None
) -> list[dict[str, object]]:
    return [
        {
            "partition": pin.partition,
            "publication_id": pin.publication_id,
            "fencing_token": pin.fencing_token,
            "digest": pin.digest,
        }
        for pin in history.pins
        if keep is None or pin.publication_id in keep
    ]


def build_input(
    builder: InputBuilder, request: BuildRequest, remaining_seconds: Callable[[], float]
) -> dict[str, object]:
    """Stage one build attempt's input and return the manifest key and the shard list."""
    if (
        _RUN_ID.fullmatch(request.run_id) is None
        or _ATTEMPT.fullmatch(request.build_attempt_id) is None
    ):
        raise InputBuildFailedError("REQUEST_INVALID")
    selected = _select(builder)
    pins = selected.pins
    if pins.input_fingerprint != request.input_fingerprint or pins.input_end is None:
        raise InputBuildFailedError("SELECTION_MOVED")
    settings = builder.settings
    policy = builder.policy.policy
    base = f"runs/{request.run_id}/input/{request.build_attempt_id}"
    plan = WeeklySeriesPlan(
        sales_publication_ids=tuple(pin.publication_id for pin in pins.sales),
        traffic_publication_ids=tuple(pin.publication_id for pin in pins.traffic),
        distributor_view=policy.input.distributor_view,
        selling_program=policy.input.selling_program,
        shard_max_asins=policy.engine.shard_max_asins,
        target_destination=f"s3://{settings.processing_bucket}/{base}/target/",
        traffic_destination=f"s3://{settings.processing_bucket}/{base}/traffic/",
    )
    source = builder.weekly(remaining_seconds)
    try:
        roster = source.roster(plan)
        if not roster.canonical_asins:
            raise InputBuildFailedError("ROSTER_EMPTY")
        _check_roster(roster.canonical_asins, roster.shards, policy.engine.shard_max_asins)
        titles = _titles(builder, selected, roster.canonical_asins)
        unloaded = source.unload(plan)
    except WeeklySeriesError as error:
        _report("build", "refused", {"code": error.code, "query_id": error.query_id})
        raise InputBuildFailedError(error.code) from None
    shards = plan_shards(
        ShardPlanRequest(roster.canonical_asins, policy.engine.shard_max_asins)
    ).shards
    titles_body = (titles.model_dump_json(indent=None) + "\n").encode("utf-8")
    titles_key = f"{base}/titles.json"
    builder.objects.put(titles_key, titles_body)
    identity = _identity(builder, request.input_fingerprint)
    window_ids = {pin.publication_id for pin in (*pins.sales, *pins.traffic)}
    published = [
        value
        for value in (
            selected.result.sales.newest_published_at,
            selected.result.traffic.newest_published_at,
        )
        if value is not None
    ]
    manifest = InputManifestV1.model_validate(
        {
            "schema_version": 1,
            "run_id": request.run_id,
            "build_attempt_id": request.build_attempt_id,
            "set": {
                "marketplace_key": settings.marketplace_key,
                "distributor_view": policy.input.distributor_view,
                "selling_program": policy.input.selling_program,
            },
            "input_end": pins.input_end.isoformat(),
            "expected_fence": 0,
            "input_fingerprint": request.input_fingerprint,
            "run_fingerprint": run_fingerprint(identity),
            "run_identity": {
                **{k: v for k, v in identity.as_json().items() if k not in {"execution_plan"}},
                "policy_section_versions": builder.policy.section_versions,
                "instance_type": settings.instance_type,
                "max_runtime_seconds": settings.max_runtime_seconds,
            },
            "pins": {
                "sales": _pin_json(selected.result.sales, window_ids),
                "traffic": _pin_json(selected.result.traffic, window_ids),
                "products": _pin_json(selected.result.products),
            },
            "shards": [
                {
                    "index": shard.index,
                    "asin_count": shard.asin_count,
                    "first_canonical_asin": shard.first_canonical_asin,
                    "last_canonical_asin": shard.last_canonical_asin,
                }
                for shard in shards
            ],
            "parquet_objects": [
                *_objects(builder, f"{base}/target/", "target"),
                *_objects(builder, f"{base}/traffic/", "traffic"),
            ],
            "titles_key": titles_key,
            "titles_sha256": hashlib.sha256(titles_body).hexdigest(),
            "coverage": {
                "sales": [
                    {"start": r.start.isoformat(), "end": r.end.isoformat()}
                    for r in pins.covered_sales
                ],
                "traffic": [
                    {"start": r.start.isoformat(), "end": r.end.isoformat()}
                    for r in pins.covered_traffic
                ],
            },
            "newest_publication_published_at": max(published).isoformat(),
            "boundary_schema_digests": boundary_schema_digests(),
        }
    )
    manifest_key = f"{base}/manifest.json"
    builder.objects.put(
        manifest_key, (manifest.model_dump_json(by_alias=True) + "\n").encode("utf-8")
    )
    return {
        "outcome": "built",
        "manifest_key": manifest_key,
        "build_attempt_id": request.build_attempt_id,
        "run_hash12": run_hash12(request.run_id),
        "asin_count": len(roster.canonical_asins),
        "shards": [{"shard": shard.index} for shard in shards],
        "statements": [_stat(stat) for stat in (roster.statement, *unloaded.statements)],
    }


def _stat(stat: StatementStat) -> dict[str, object]:
    return {
        "kind": stat.kind,
        "query_id": stat.query_id,
        "bytes_scanned": stat.bytes_scanned,
        "engine_millis": stat.engine_millis,
        "total_millis": stat.total_millis,
    }


# ---- deployed composition ----


def _report(phase: str, outcome: str, details: Mapping[str, object]) -> None:
    report_operation(
        OperationLog(
            operation=f"demand_forecast.input.{phase}",
            component="demand-forecast-input",
            phase=phase,
            outcome=outcome,
            severity=LogSeverity.INFO if outcome != "refused" else LogSeverity.WARNING,
            details=details,
        ),
        _LOGGER,
    )


class _S3Objects:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def list_sizes(self, prefix: str) -> dict[str, int]:
        found: dict[str, int] = {}
        for page in self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix
        ):
            for item in page.get("Contents", []):
                found[item.get("Key", "")] = item.get("Size", 0)
        return found

    def sha256(self, key: str) -> str:
        digest = hashlib.sha256()
        body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"]
        for chunk in body.iter_chunks(chunk_size=_CHUNK):
            digest.update(chunk)
        return digest.hexdigest()

    def tail(self, key: str, size: int) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key, Range=f"bytes=-{size}")
        return response["Body"].read()

    def put(self, key: str, body: bytes) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType="application/json"
        )


@lru_cache(maxsize=1)
def _builder() -> InputBuilder:
    settings = demand_forecast_input_settings()
    root = _root()
    contracts = tuple(contract for _, contract in load_contracts(root))
    packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    current = DynamoCurrentPublications(settings.semantic_publications_table)
    s3 = boto3.client("s3")
    target = AthenaWeeklySeriesTarget(
        settings.athena_batch_workgroup,
        settings.semantic_v2_current_database,
        boto3.client("athena"),
        s3,
    )
    return InputBuilder(
        settings=settings,
        policy=load_policy(root / "contracts/forecasting/demand-forecast.yaml"),
        bridge=bridge,
        selection=ForecastHistorySelector(current, packs, contracts, bridge),
        titles=CanonicalRetailProductHierarchy(
            current,
            S3CanonicalAdapter(settings.semantic_v2_bucket),
            packs,
            contracts,
            bridge,
            marketplace_key=settings.marketplace_key,
        ),
        weekly=lambda remaining: AthenaWeeklySeriesSource(target, remaining),
        objects=_S3Objects(s3, settings.processing_bucket),
        today=lambda: datetime.now(UTC).date(),
    )


def _int(event: Mapping[str, Any], key: str) -> int:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputBuildFailedError("REQUEST_INVALID")
    return value


@trace_boundary(DEMAND_FORECAST_SELECT, DEMAND_FORECAST_BUILD)
@flushes_traces
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, object]:
    """Dispatch `select` or `build` from the state machine (or a dev probe)."""
    operation = event.get("operation")
    remaining = cast(
        "Callable[[], int]", getattr(context, "get_remaining_time_in_millis", lambda: 900_000)
    )
    if operation == "select":
        with span(DEMAND_FORECAST_SELECT, operation="select") as recording:
            result = select_input(_builder())
            recording.record(outcome=str(result["outcome"]))
            _report("select", str(result["outcome"]), result)
            return result
    if operation == "build":
        request = BuildRequest(
            run_id=str(event.get("run_id", "")),
            redrive_count=_int(event, "redrive_count"),
            retry_count=_int(event, "retry_count"),
            input_fingerprint=str(event.get("input_fingerprint", "")),
        )
        with span(DEMAND_FORECAST_BUILD, operation="build") as recording:
            started = datetime.now(UTC)
            result = build_input(_builder(), request, lambda: remaining() / 1000)
            duration = round((datetime.now(UTC) - started).total_seconds() * 1000)
            recording.record(
                outcome="built",
                series_count=cast("int", result["asin_count"]),
                duration_ms=duration,
            )
            _report("build", "built", result)
            return result
    raise InputBuildFailedError("REQUEST_INVALID")
