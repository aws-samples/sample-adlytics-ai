# SPDX-License-Identifier: MIT-0
"""The demand-forecast input builder: `select_input()` and `build_input()` (design §3c, §6d).

One Lambda (`ForecastInputFunction`) serves both Step Functions states.

- **select** applies §6d steps 1 to 5. It checks the marketplace key against the commerce
  identity bridge (`MARKETPLACE_UNKNOWN`), reads the set's `CURRENT` (one consistent GetItem)
  and the CURRENT pins of the 1,000 days ending the check date, picks `input_end`, and decides
  `run`, `no_op_same_input` or `held_input_not_ready` (`no_complete_week`, `input_end_too_old`,
  `weekly_slot_not_due`). A scheduled check's date and run ID come from its `scheduled_time`
  (OQ-S2-3); an operator start's run ID is its execution name and its date is today. Off the slot
  weekday, the latest slot run's `#select` receipt is the one further GetItem (OQ-S2-1). Every
  call emits `ForecastCurrentAgeDays` (when a generation is CURRENT) and `UnknownSalesDays`.
- **build** re-reads the selection and refuses `SELECTION_MOVED` if its input fingerprint moved;
  reads the roster and cross-checks it against `plan_shards()` (`SHARD_PLAN_MISMATCH`,
  `ROSTER_TOO_LARGE`); reads the forecast marketplace's product titles (`SELECTION_MOVED` when
  the hierarchy's publications differ from the product pins; all-null titles when there is no
  hierarchy); runs the two partitioned UNLOADs into the attempt's fresh prefix
  `runs/<run_id>/input/r<RedriveCount>a<RetryCount>/`; gives every shard the traffic `UNLOAD`
  wrote nothing for a zero-row traffic object; and writes `titles.json` (`TitleMapV1`) and
  `manifest.json` (`InputManifestV1`, with the `expected_fence` select read), both validated
  before they are written.

A refusal raises `InputBuildFailedError` whose message is the failure category, so the state machine
and an operator read the same code.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
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
    empty_traffic_parquet,
    parquet_row_count,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.demand_forecast_policy import LoadedPolicy, load_policy
from adapters.outbound.dynamodb.demand_forecast_store import DynamoDbForecastStore
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.publication_selection import ForecastHistorySelector
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.demand_forecast_metrics import (
    CloudWatchDemandForecastMetrics,
    Datapoint,
    current_age,
    unknown_days,
)
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from adapters.outbound.telemetry.tracing import (
    DEMAND_FORECAST_BUILD,
    DEMAND_FORECAST_SELECT,
    span,
    trace_boundary,
)
from domain.forecasting.boundary import (
    ForecastSetV1,
    InputManifestV1,
    TitleMapV1,
    boundary_schema_digests,
)
from domain.forecasting.fingerprint import RunIdentity, run_fingerprint, run_hash12
from domain.forecasting.identity import ProductKeyError, display_map
from domain.forecasting.models import CHRONOS2_MODEL_ID
from domain.forecasting.publish import (
    CurrentPointer,
    forecast_set_id,
    operator_reason_valid,
)
from domain.forecasting.selection import (
    SELECTION_DAYS,
    SelectDecisionRequest,
    SelectionWindowRequest,
    SelectOutcome,
    Trigger,
    WindowPin,
    WindowPins,
    claims_slot,
    current_age_days,
    scheduled_run_id,
    select_decision,
    settle_slot,
    slot_date,
    unknown_sales_days,
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
from ports.outbound.forecast_store import SlotClaimRequest, SlotClaimResult
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
_RUN_ID_MAX: Final = 80
"""Step Functions' execution-name limit [verified API_StartExecution, read 2026-11-16]."""
_RUN_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ATTEMPT: Final = re.compile(r"^r[0-9]{1,4}a[0-9]{1,4}$")
_OPERATOR_RUN_ID: Final = re.compile(
    r"^(?P<set>[0-9a-f]{12})-[0-9]{4}-[0-9]{2}-[0-9]{2}-op-[1-9][0-9]*$"
)
_SHARD_PATH: Final = re.compile(r"/shard=([0-9]{1,3})/")
_TAIL_BYTES: Final = 64 * 1024
_CHUNK: Final = 1024 * 1024
EMPTY_TRAFFIC_OBJECT: Final = "no-traffic-rows.parquet"


class InputBuildFailedError(Exception):
    """A refusal; the message is the failure category the state machine and operator read."""


def _root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "contracts").is_dir():
            return candidate
    raise InputBuildFailedError("CONTRACTS_UNAVAILABLE")


# ---- composition seams (fakes in unit tests) ----


class PointerReads(Protocol):
    """The forecast-table calls `select` makes (`ForecastStorePort` satisfies it)."""

    def get_current(self, set_id: str) -> CurrentPointer | None:
        """Read the set's `CURRENT` item, strongly consistent."""
        ...

    def claim_slot(self, request: SlotClaimRequest) -> SlotClaimResult:
        """Write the period's slot marker if absent, else name the run that holds it."""
        ...


class MetricsSink(Protocol):
    """Where operational datapoints go."""

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        """Put every datapoint at `timestamp`."""
        ...


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
    store: PointerReads
    metrics: MetricsSink
    now: Callable[[], datetime]

    @property
    def set_id(self) -> str:
        """The one set's ID, as the publisher derives it (`forecast_set_id()`)."""
        policy = self.policy.policy.input
        return forecast_set_id(
            ForecastSetV1(
                marketplace_key=self.settings.marketplace_key,
                distributor_view=policy.distributor_view,
                selling_program=policy.selling_program,
            )
        )


@dataclass(frozen=True)
class SelectRequest:
    """One `select` invocation: the execution and, for a scheduled check, its scheduled time.

    A scheduled input carries `scheduled_time` and never `RETRY_FAILED` or a reason (§6d step
    3). An operator start is named `<set hash 12>-<UTC date>-op-<n>`; `RETRY_FAILED` needs a
    reason of 1 to 200 printable characters.
    """

    execution_name: str
    scheduled_time: datetime | None = None
    retry_failed: bool = False
    operator_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.execution_name:
            raise InputBuildFailedError("REQUEST_INVALID")
        if self.scheduled_time is not None:
            if self.scheduled_time.tzinfo is None:
                raise InputBuildFailedError("REQUEST_INVALID")
            if self.retry_failed or self.operator_reason is not None:
                raise InputBuildFailedError("REQUEST_INVALID")
            return
        if (
            len(self.execution_name) > _RUN_ID_MAX
            or _OPERATOR_RUN_ID.fullmatch(self.execution_name) is None
        ):
            raise InputBuildFailedError("REQUEST_INVALID")
        reason = self.operator_reason
        if reason is None:
            if self.retry_failed:
                raise InputBuildFailedError("OPERATOR_REASON_INVALID")
            return
        if not operator_reason_valid(reason):
            raise InputBuildFailedError("OPERATOR_REASON_INVALID")

    @property
    def trigger(self) -> Trigger:
        """Scheduled when the input carries a scheduled time; otherwise an operator start."""
        return Trigger.OPERATOR if self.scheduled_time is None else Trigger.SCHEDULED


@dataclass(frozen=True)
class BuildRequest:
    """One build invocation: the run, its attempt and the selection's input fingerprint."""

    run_id: str
    redrive_count: int
    retry_count: int
    input_fingerprint: str
    expected_fence: int
    """The `CURRENT` fence `select` read; the manifest carries it to the commit (§6b)."""

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


def _select(builder: InputBuilder, today: date) -> _Selected:
    key = builder.settings.marketplace_key
    if key not in {mapping.target.value for mapping in builder.bridge.marketplace_mappings}:
        raise InputBuildFailedError("MARKETPLACE_UNKNOWN")
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


def _identity_json(builder: InputBuilder, identity: RunIdentity) -> dict[str, object]:
    """Return the `RunIdentityV1` form a manifest and a select receipt carry."""
    settings = builder.settings
    return {
        **{k: v for k, v in identity.as_json().items() if k not in {"execution_plan"}},
        "policy_section_versions": builder.policy.section_versions,
        "instance_type": settings.instance_type,
        "max_runtime_seconds": settings.max_runtime_seconds,
    }


def _run_id(builder: InputBuilder, request: SelectRequest, check_date: date) -> str:
    if request.trigger is Trigger.SCHEDULED:
        return scheduled_run_id(builder.set_id, check_date)
    match = _OPERATOR_RUN_ID.fullmatch(request.execution_name)
    if match is None or match["set"] != builder.set_id:
        raise InputBuildFailedError("REQUEST_INVALID")
    return request.execution_name


@dataclass(frozen=True)
class _Check:
    """One check's request, its UTC date and the `CURRENT` it read."""

    request: SelectRequest
    check_date: date
    current: CurrentPointer | None


def _request(builder: InputBuilder, check: _Check, fingerprint: str) -> SelectDecisionRequest:
    return SelectDecisionRequest(
        run_fingerprint=fingerprint,
        current=check.current,
        trigger=check.request.trigger,
        check_date=check.check_date,
        admission_weekday=builder.policy.policy.retry.admission_weekday,
        retry_failed=check.request.retry_failed,
    )


def _claim(builder: InputBuilder, check: _Check, run_id: str) -> SlotClaimResult:
    """Claim this slot period's marker; a scheduled check always has its scheduled time."""
    scheduled = check.request.scheduled_time
    if scheduled is None:
        raise InputBuildFailedError("REQUEST_INVALID")
    return builder.store.claim_slot(
        SlotClaimRequest(
            set_id=builder.set_id,
            slot_date=slot_date(check.check_date, builder.policy.policy.retry.admission_weekday),
            run_id=run_id,
            execution_name=check.request.execution_name,
            scheduled_time=scheduled,
        )
    )


def _emit_check_metrics(
    builder: InputBuilder, check: _Check, pins: WindowPins
) -> dict[str, object]:
    now = builder.now()
    grace = builder.policy.policy.missing_weeks.unknown_day_grace_days
    unknown = unknown_sales_days(pins, check.check_date, grace)
    points = [unknown_days(unknown)]
    age = None
    current = check.current
    if current is not None and current.published_at is not None:
        age = current_age_days(current.published_at, now)
        points.append(current_age(age))
    builder.metrics.emit(points, now)
    return {"current_age_days": age, "unknown_sales_days": unknown}


def select_input(builder: InputBuilder, request: SelectRequest) -> dict[str, object]:
    """Decide `run`, `no_op_same_input` or `held_input_not_ready` (design §6d steps 1 to 5)."""
    scheduled = request.scheduled_time
    check_date = (
        builder.now().astimezone(UTC).date()
        if scheduled is None
        else (scheduled.astimezone(UTC).date())
    )
    run_id = _run_id(builder, request, check_date)
    current = builder.store.get_current(builder.set_id)
    check = _Check(request, check_date, current)
    selected = _select(builder, check_date)
    pins = selected.pins
    common: dict[str, object] = {
        "run_id": run_id,
        "trigger": request.trigger.value,
        "check_date": check_date.isoformat(),
        "expected_fence": 0 if current is None else current.fence,
        "operator_reason": request.operator_reason,
        "input_end": None if pins.input_end is None else pins.input_end.isoformat(),
    }
    if pins.held is not None or pins.input_fingerprint is None or pins.input_end is None:
        metrics = _emit_check_metrics(builder, check, pins)
        return {
            "outcome": SelectOutcome.HELD_INPUT_NOT_READY.value,
            "category": None if pins.held is None else pins.held.value,
            **common,
            **metrics,
        }
    identity = _identity(builder, pins.input_fingerprint)
    fingerprint = run_fingerprint(identity)
    decision_request = _request(builder, check, fingerprint)
    decision = select_decision(decision_request)
    metrics = _emit_check_metrics(builder, check, pins)
    # The claim is the last write, after the metrics, so a select that fails leaves the slot free
    # for a later day (R7-M2); only a check that returns its decision consumes it.
    holder = None
    if claims_slot(decision_request, decision):
        claim = _claim(builder, check, run_id)
        decision = settle_slot(decision_request, decision, claimed=claim.claimed)
        holder = claim.holder_run_id
    result: dict[str, object] = {
        "outcome": decision.outcome.value,
        "category": None if decision.category is None else decision.category.value,
        "basis": decision.basis.value,
        **common,
        "run_fingerprint": fingerprint,
        # Every component, the input fingerprint included, for a no-op's or a slot hold's
        # receipt (slice-2 acceptance 2, R1-12).
        "run_identity": _identity_json(builder, identity),
        "slot_holder": holder,
        **metrics,
    }
    if decision.outcome is SelectOutcome.RUN:
        result |= {
            "input_fingerprint": pins.input_fingerprint,
            "sales_pins": len(pins.sales),
            "traffic_pins": len(pins.traffic),
        }
    return result


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


def _write_empty_traffic(builder: InputBuilder, base: str, shards: tuple[int, ...]) -> None:
    """Give every shard the traffic `UNLOAD` wrote nothing for a zero-row traffic object.

    Traffic is an optional covariate (design §3d): a shard with no glance-view rows still
    forecasts, and the job and its mount always find one traffic object per shard.
    """
    written: set[int] = set()
    for key, size in builder.objects.list_sizes(f"{base}/traffic/").items():
        match = _SHARD_PATH.search(key)
        if match is not None and size > 0:
            written.add(int(match.group(1)))
    for shard in shards:
        if shard not in written:
            builder.objects.put(
                f"{base}/traffic/shard={shard}/{EMPTY_TRAFFIC_OBJECT}", empty_traffic_parquet()
            )


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
    if isinstance(request.expected_fence, bool) or request.expected_fence < 0:
        raise InputBuildFailedError("REQUEST_INVALID")
    selected = _select(builder, builder.now().astimezone(UTC).date())
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
    _write_empty_traffic(builder, base, tuple(shard.index for shard in shards))
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
            "expected_fence": request.expected_fence,
            "input_fingerprint": request.input_fingerprint,
            "run_fingerprint": run_fingerprint(identity),
            "run_identity": _identity_json(builder, identity),
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
        kind = "application/octet-stream" if key.endswith(".parquet") else "application/json"
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType=kind)


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
    policy = load_policy(root / "contracts/forecasting/demand-forecast.yaml")
    return InputBuilder(
        settings=settings,
        policy=policy,
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
        store=DynamoDbForecastStore(settings.forecast_table, policy.policy.serving.item_max_bytes),
        metrics=CloudWatchDemandForecastMetrics(boto3.client("cloudwatch")),
        now=lambda: datetime.now(UTC),
    )


def _int(event: Mapping[str, Any], key: str) -> int:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputBuildFailedError("REQUEST_INVALID")
    return value


def select_request(event: Mapping[str, Any]) -> SelectRequest:
    """Parse `SelectInput`'s input: the execution name and the scheduled or operator fields."""
    name = event.get("execution_name")
    scheduled = event.get("scheduled_time")
    retry = event.get("retry_failed", False)
    reason = event.get("operator_reason")
    if (
        not isinstance(name, str)
        or not isinstance(retry, bool)
        or (scheduled is not None and not isinstance(scheduled, str))
        or (reason is not None and not isinstance(reason, str))
    ):
        raise InputBuildFailedError("REQUEST_INVALID")
    at = None
    if scheduled is not None:
        try:
            at = datetime.fromisoformat(scheduled)
        except ValueError:
            raise InputBuildFailedError("REQUEST_INVALID") from None
    return SelectRequest(name, at, retry, reason)


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
            result = select_input(_builder(), select_request(event))
            recording.record(outcome=str(result["outcome"]))
            _report("select", str(result["outcome"]), result)
            return result
    if operation == "build":
        request = BuildRequest(
            run_id=str(event.get("run_id", "")),
            redrive_count=_int(event, "redrive_count"),
            retry_count=_int(event, "retry_count"),
            input_fingerprint=str(event.get("input_fingerprint", "")),
            expected_fence=_int(event, "expected_fence"),
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
