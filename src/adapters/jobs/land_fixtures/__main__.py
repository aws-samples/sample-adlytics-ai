# SPDX-License-Identifier: MIT-0
"""Land the fixture deliveries in one explicit dev V2 raw zone."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import FrameType
from typing import cast
from uuid import uuid4

import boto3
from aws_lambda_powertools import Logger

from adapters.amc_csv import dict_rows
from adapters.config import TracingSettings
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.fixture_generation.provenance import (
    load_fixture_evidence_manifest,
    validate_fixture_provenance,
)
from adapters.jobs.land_fixtures.bundle import (
    AMC_FIXTURE_CONTRACTS,
    DeliveryClass,
    LandedDelivery,
    PlanSelection,
    ProbeRetrieval,
    ProbeRetrievalRequest,
    manifest_facts,
    named_cadence_delivery,
    plan_landings,
    plan_probe_retrieval,
    plan_with_backfill,
)
from adapters.jobs.land_fixtures.job import (
    ApplyLandFixturesRequest,
    LandFixturesCancelledError,
    LandFixturesProgress,
    SealSequenceGuard,
    SealWait,
    apply_immutable,
    check_settled_report,
)
from adapters.jobs.land_fixtures.twin import (
    ProbeTwinRequest,
    send_probe_twin,
    verify_reserved_probe,
)
from adapters.jobs.reprocess_semantic_v2.resources import (
    NamedRawResourceRequest,
    resolve_named_semantic_resources,
    verify_named_raw_bucket,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.telemetry.bootstrap import configure_tracing, flush_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import LAND_FIXTURES_PROCESS, span, trace_boundary
from domain.curation.landing import RETRIEVED_FORMAT, LandingError
from domain.operations.run_request import HistoricalPolicy, is_historical
from domain.semantics.publication import SuccessfulPublication

ROOT = pathlib.Path(__file__).resolve().parents[4]
DEV_ONLY = "dev"
LOGGER = Logger(service="adlytics-land-fixtures", level="DEBUG")
_OPERATION = LAND_FIXTURES_PROCESS
_COMPONENT = "land_fixtures.operator"
_HEARTBEAT_INTERVAL_SECONDS = 30.0


class NotDevError(Exception):
    """The landing refused because it was pointed at something other than dev."""


@dataclass(frozen=True, slots=True)
class _Arguments:
    """Validated command-line arguments selected before any AWS access."""

    profile: str
    region: str
    expected_account: str
    environment: str
    operation_id: str
    trace: bool
    stack: str
    bucket: str
    predecessor_execution_id: str | None
    dry_run: bool
    selection: PlanSelection | None = None
    settled_report: pathlib.Path | None = None
    probe_retrieval: str | None = None
    """One named phase-A delivery to copy to a newer retrieval of its window (V-24)."""
    no_twin: bool = False
    """Put only; send no `on_demand` twin request (accepted only with `probe_retrieval`)."""
    delivery: str | None = None
    """Land exactly one named non-historical cadence delivery, at its planned key."""
    graph_store_stack: str | None = None
    """S6 (V-19): the store stack owning the graph table the probe twin is decided in."""
    reserved_probe_key: str | None = None
    """D7: the exact dry-planned probe key this invocation reserves or sends the twin for."""
    twin_only: bool = False
    """D7: put nothing; verify the reserved key's one immutable version and send its twin."""


class _Heartbeat:
    """Repeat only the last durable fixture-delivery progress snapshot."""

    def __init__(
        self,
        reporter: Callable[[LandFixturesProgress], None],
        initial: LandFixturesProgress,
        *,
        interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._reporter = reporter
        self._progress = initial
        self._interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, progress: LandFixturesProgress) -> None:
        with self._lock:
            if progress.completed < self._progress.completed:
                raise ValueError("fixture landing progress cannot move backwards.")
            self._progress = progress

    def emit_once(self) -> None:
        with self._lock:
            progress = self._progress
        self._reporter(progress)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="land-fixtures-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self.emit_once()


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _profile(value: str) -> str:
    normalized = _non_blank(value)
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def _account(value: str) -> str:
    normalized = _non_blank(value)
    if re.fullmatch(r"\d{12}", normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def _parse_args(argv: tuple[str, ...]) -> _Arguments:
    """Parse one explicit immutable V2 landing invocation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--env", required=True, type=_non_blank)
    parser.add_argument("--operation-id", required=True, type=_non_blank)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--stack", required=True, type=_non_blank)
    parser.add_argument("--bucket", required=True, type=_non_blank)
    parser.add_argument("--predecessor-execution-id", type=_non_blank)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--classes",
        help="comma-separated delivery classes to land: cadence, whole, trap, seal (design §2.3)",
    )
    parser.add_argument("--run-days-from", type=date.fromisoformat)
    parser.add_argument("--run-days-to", type=date.fromisoformat)
    parser.add_argument(
        "--settled-report",
        type=pathlib.Path,
        help="probe_pipeline_settled.py report, at most 15 minutes old; required to land a seal",
    )
    parser.add_argument(
        "--probe-retrieval",
        action="append",
        type=_non_blank,
        help="copy exactly one named phase-A cadence delivery to the next free probe retrieval",
    )
    parser.add_argument(
        "--no-twin",
        action="store_true",
        help="with --probe-retrieval only: put the key and send no on_demand twin request",
    )
    parser.add_argument(
        "--graph-store-stack",
        type=_non_blank,
        help="with --probe-retrieval: the store stack the on_demand twin is decided against",
    )
    parser.add_argument(
        "--reserved-probe-key",
        type=_non_blank,
        help="with --probe-retrieval: the exact dry-planned probe key to reserve or twin (D7)",
    )
    parser.add_argument(
        "--twin-only",
        action="store_true",
        help="with --reserved-probe-key: verify the reserved key and send only its twin",
    )
    parser.add_argument(
        "--delivery",
        action="append",
        type=_non_blank,
        help="land exactly one named non-historical cadence delivery at its planned key",
    )
    parsed = parser.parse_args(argv)
    if parsed.predecessor_execution_id is not None and parsed.dry_run:
        parser.error("--predecessor-execution-id cannot be used with --dry-run")
    probe = _probe_name(parser, parsed)
    _reservation(parser, parsed, probe)
    if (
        probe is not None
        and not parsed.no_twin
        and not parsed.dry_run
        and parsed.graph_store_stack is None
    ):
        parser.error("--probe-retrieval sends an on_demand twin: give --graph-store-stack")
    delivery = _delivery_name(parser, parsed)
    selection = _selection(parser, parsed)
    return _Arguments(
        profile=str(parsed.profile),
        region=str(parsed.region),
        expected_account=str(parsed.expected_account),
        environment=str(parsed.env),
        operation_id=str(parsed.operation_id),
        trace=bool(parsed.trace),
        stack=str(parsed.stack),
        bucket=str(parsed.bucket),
        predecessor_execution_id=(
            None
            if parsed.predecessor_execution_id is None
            else str(parsed.predecessor_execution_id)
        ),
        dry_run=bool(parsed.dry_run),
        selection=selection,
        settled_report=cast("pathlib.Path | None", parsed.settled_report),
        probe_retrieval=probe,
        no_twin=bool(parsed.no_twin),
        delivery=delivery,
        graph_store_stack=(
            None if parsed.graph_store_stack is None else str(parsed.graph_store_stack)
        ),
        reserved_probe_key=(
            None if parsed.reserved_probe_key is None else str(parsed.reserved_probe_key)
        ),
        twin_only=bool(parsed.twin_only),
    )


def _reservation(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace, probe: str | None
) -> None:
    """Constrain --reserved-probe-key and --twin-only to their two D7 shapes.

    Reserve: `--probe-retrieval N --reserved-probe-key K --no-twin` puts K only if K is still
    the planned key. Launch: `--probe-retrieval N --reserved-probe-key K --twin-only
    --graph-store-stack S` puts nothing and sends K's twin.
    """
    reserved = cast("str | None", parsed.reserved_probe_key)
    if bool(parsed.twin_only) and reserved is None:
        parser.error("--twin-only requires --reserved-probe-key")
    if reserved is None:
        return
    if probe is None:
        parser.error("--reserved-probe-key is accepted only with --probe-retrieval")
    if parsed.dry_run:
        parser.error("--reserved-probe-key cannot be used with --dry-run")
    if parsed.twin_only == parsed.no_twin:
        parser.error("--reserved-probe-key needs exactly one of --no-twin or --twin-only")
    if parsed.twin_only and parsed.graph_store_stack is None:
        parser.error("--twin-only sends an on_demand twin: give --graph-store-stack")


def _delivery_name(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> str | None:
    """Narrow --delivery to one name, refusing any other selection beside it."""
    names = cast("list[str] | None", parsed.delivery)
    if names is None:
        return None
    if len(names) != 1:
        parser.error("--delivery names exactly one delivery")
    if (
        parsed.probe_retrieval is not None
        or parsed.classes is not None
        or parsed.run_days_from is not None
        or parsed.run_days_to is not None
        or parsed.settled_report is not None
    ):
        parser.error("--delivery takes no other selection")
    return names[0]


def _probe_name(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> str | None:
    """Narrow --probe-retrieval to one name, refusing every selection beside it (V-24)."""
    names = cast("list[str] | None", parsed.probe_retrieval)
    if names is None:
        if parsed.no_twin:
            parser.error("--no-twin is accepted only with --probe-retrieval")
        return None
    if len(names) != 1:
        parser.error("--probe-retrieval names exactly one delivery")
    if (
        parsed.classes is not None
        or parsed.run_days_from is not None
        or parsed.run_days_to is not None
    ):
        parser.error("--probe-retrieval takes no class or run-day selection")
    if parsed.predecessor_execution_id is not None or parsed.settled_report is not None:
        parser.error("--probe-retrieval takes no predecessor or settled report")
    return names[0]


def _taken_retrievals(session: boto3.Session, bucket: str, window: str) -> frozenset[datetime]:
    """List every `retrieved=` instant already under one window prefix (read only)."""
    client = session.client("s3")
    taken: set[datetime] = set()
    pages = client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{window}retrieved="
    )
    for page in pages:
        for item in page.get("Contents", []):
            segment = item.get("Key", "").removeprefix(f"{window}retrieved=").split("/", 1)[0]
            try:
                taken.add(datetime.strptime(segment, RETRIEVED_FORMAT).replace(tzinfo=UTC))
            except ValueError:
                continue
    return frozenset(taken)


def _plan_probe(
    arguments: _Arguments,
    session: boto3.Session,
    plan: tuple[tuple[LandedDelivery, ...], HistoricalPolicy],
) -> ProbeRetrieval:
    """Plan the one probe copy against the window's existing retrievals, before any put."""
    name = cast("str", arguments.probe_retrieval)
    planned, policy = plan
    request = ProbeRetrievalRequest(name=name, taken=frozenset())
    window = plan_probe_retrieval(planned, policy, request).window
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != arguments.expected_account:
        raise PermissionError("caller account does not match the explicitly expected account")
    taken = _taken_retrievals(session, arguments.bucket, window)
    if arguments.twin_only:
        # The reserved key occupies its own slot; it must still be the first free one without it.
        taken -= {_reserved_instant(cast("str", arguments.reserved_probe_key), window)}
    planned_probe = plan_probe_retrieval(
        planned, policy, ProbeRetrievalRequest(name=name, taken=taken)
    )
    reserved = arguments.reserved_probe_key
    if reserved is not None and planned_probe.probe.manifest.manifest_key != reserved:
        raise LandingError("the reserved probe key is not the planned probe key (plan drift).")
    return planned_probe


def _reserved_instant(key: str, window: str) -> datetime:
    """Return the `retrieved=` instant of a reserved key under its window, or refuse it."""
    segment = key.removeprefix(f"{window}retrieved=").split("/", 1)[0]
    if not key.startswith(f"{window}retrieved=") or not segment:
        raise LandingError("the reserved probe key is not under the delivery's window.")
    try:
        return datetime.strptime(segment, RETRIEVED_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise LandingError("the reserved probe key has no retrieval instant.") from None


def probe_report(retrieval: ProbeRetrieval, *, no_twin: bool) -> dict[str, object]:
    """Return the one machine-readable dry-run line the probe runner reads (R2-13)."""
    probe = retrieval.probe
    (data,) = probe.data
    return {
        "deliveries": 1,
        "name": retrieval.original.fixture,
        "original_key": retrieval.original.manifest.manifest_key,
        "key": probe.manifest.manifest_key,
        "data_key": data.key,
        "window": retrieval.window,
        "r": probe.manifest.retrieved_at.strftime(RETRIEVED_FORMAT),
        "original_r": retrieval.original.manifest.retrieved_at.strftime(RETRIEVED_FORMAT),
        "body_sha256": hashlib.sha256(data.body).hexdigest(),
        "original_body_sha256": hashlib.sha256(retrieval.original.data[0].body).hexdigest(),
        "historical": is_historical(manifest_facts(probe.manifest), retrieval.policy),
        "original_historical": is_historical(
            manifest_facts(retrieval.original.manifest), retrieval.policy
        ),
        "no_twin": no_twin,
    }


def _selection(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> PlanSelection | None:
    """Narrow --classes and the run-day bounds, refusing every unusable combination."""
    run_from = cast("date | None", parsed.run_days_from)
    run_to = cast("date | None", parsed.run_days_to)
    raw = cast("str | None", parsed.classes)
    if raw is None:
        if run_from is not None or run_to is not None:
            parser.error("--run-days-from and --run-days-to require --classes")
        return None
    names = [item.strip() for item in raw.split(",") if item.strip()]
    known = {item.value for item in DeliveryClass}
    unknown = sorted(set(names) - known)
    if not names or unknown:
        parser.error(
            f"--classes names an unknown delivery class: {', '.join(unknown) or '(none)'}; "
            f"expected any of {', '.join(sorted(known))}"
        )
    if run_from is not None and run_to is not None and run_from > run_to:
        parser.error("--run-days-from is after --run-days-to")
    return PlanSelection(frozenset(DeliveryClass(name) for name in names), run_from, run_to)


def _earliest_landing(deliveries: tuple[LandedDelivery, ...]) -> datetime:
    """Return the selected deliveries' largest retrieval instant; no put may come before it."""
    return max(delivery.manifest.retrieved_at for delivery in deliveries)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_settled(arguments: _Arguments, account: str, now: datetime) -> None:
    """Refuse a seal landing without a fresh settled report (plan D1)."""
    if arguments.settled_report is None:
        raise LandingError(
            "a selection with seal deliveries needs --settled-report from "
            "probe_pipeline_settled.py, at most 15 minutes old."
        )
    try:
        document: object = json.loads(arguments.settled_report.read_text())
    except (OSError, ValueError) as error:
        raise LandingError(f"the settled report is unreadable: {type(error).__name__}.") from None
    check_settled_report(document, environment=arguments.environment, account=account, now=now)


def _seal_current_reader(
    session: boto3.Session, environment: str
) -> Callable[[LandedDelivery], SuccessfulPublication | None]:
    """Build the guard's read-only CURRENT view, with the table the DataV2 stack owns.

    The publications table is resolved through its SSM path and checked against the named
    DataV2 stack, never guessed. A seal delivery's scope is its contract's governed scope for
    the one marketplace its rows carry, and the partition read is the delivery's last bucket.
    """
    resources = resolve_named_semantic_resources(
        environment, session.client("cloudformation"), session.client("ssm")
    )
    current = DynamoCurrentPublications(resources["publications"], session.client("dynamodb"))
    contracts = {contract.contract_id: contract for _, contract in load_contracts(ROOT)}
    packs = {pack.contract_id: pack for pack in load_source_packs(ROOT)}
    bridge = load_commerce_identity_bridge(ROOT)

    def read(delivery: LandedDelivery) -> SuccessfulPublication | None:
        contract = contracts[AMC_FIXTURE_CONTRACTS[delivery.fixture.split("#", 1)[0]]]
        column = next(
            item.source_path for item in contract.fields if item.target_column == "marketplace_key"
        )
        _header, rows = dict_rows(delivery.data[0].body.decode())
        marketplaces = {row[column] for row in rows}
        if len(marketplaces) != 1:
            raise LandingError(f"{delivery.fixture} does not carry exactly one marketplace.")
        (scope,) = publication_scopes_for(
            PublicationScopeRequest(
                packs[contract.contract_id],
                contract,
                bridge,
                {"marketplace_key": marketplaces.pop()},
            )
        )
        matches = [
            publication
            for publication in current.current_for_scope(scope)
            if publication.partition.bucket.window.end == delivery.manifest.period_end
        ]
        return max(matches, key=lambda item: item.published_at) if matches else None

    return read


def _plan_digest(deliveries: tuple[LandedDelivery, ...]) -> str:
    """Digest every ordered object key and exact body without exposing either."""
    digest = hashlib.sha256()
    for delivery in deliveries:
        for planned in delivery.ordered:
            key = planned.key.encode()
            digest.update(len(key).to_bytes(8, "big"))
            digest.update(key)
            digest.update(len(planned.body).to_bytes(8, "big"))
            digest.update(planned.body)
    return f"sha256:{digest.hexdigest()}"


def _details(progress: LandFixturesProgress, **extra: object) -> dict[str, object]:
    details: dict[str, object] = {
        "operation_id": progress.operation_id,
        "execution_id": progress.execution_id,
        "predecessor_execution_id": progress.predecessor_execution_id or "none",
        "plan_digest": progress.plan_digest,
        "stage": progress.stage,
        "completed": progress.completed,
        "total": progress.total,
        **extra,
    }
    return details


def _lifecycle(  # noqa: PLR0913
    progress: LandFixturesProgress,
    *,
    phase: str,
    outcome: str,
    severity: LogSeverity,
    **extra: object,
) -> None:
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase=phase,
            outcome=outcome,
            severity=severity,
            details=_details(progress, **extra),
        ),
        LOGGER,
    )


type _SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


def _install_cancellation_signals(
    cancelled: threading.Event,
) -> dict[signal.Signals, _SignalHandler]:
    previous: dict[signal.Signals, _SignalHandler] = {}

    def request_cancellation(_signum: int, _frame: FrameType | None) -> None:
        cancelled.set()

    for selected in (signal.SIGINT, signal.SIGTERM):
        previous[selected] = signal.getsignal(selected)
        signal.signal(selected, request_cancellation)
    return previous


def _restore_cancellation_signals(previous: dict[signal.Signals, _SignalHandler]) -> None:
    for selected, handler in previous.items():
        if handler is not None:
            signal.signal(selected, handler)


@trace_boundary(LAND_FIXTURES_PROCESS)
def main(  # noqa: PLR0912, PLR0915
    argv: tuple[str, ...] | None = None, now: Callable[[], datetime] = _utc_now
) -> int:
    """Plan immutable V2 landings, then safely apply them unless asked only to plan.

    The clock is read once, for one check: a real run refuses before any write while the UTC
    time is before the selection's largest `retrieved_at` (design §2.2, R6-10). The generator
    reads no clock.
    """
    arguments = _parse_args(tuple(sys.argv[1:]) if argv is None else argv)
    env_name = arguments.environment
    catalog = load_synthetic_entity_catalog(ROOT)
    validate_fixture_provenance(ROOT, load_fixture_evidence_manifest(ROOT), catalog)
    probe: ProbeRetrieval | None = None
    planned_all, policy = plan_with_backfill(ROOT)
    if arguments.probe_retrieval is not None:
        try:
            probe = _plan_probe(
                arguments,
                boto3.Session(profile_name=arguments.profile, region_name=arguments.region),
                (planned_all, policy),
            )
        except (LandingError, PermissionError) as error:
            raise SystemExit(f"probe retrieval refused: {error}") from None
        deliveries: tuple[LandedDelivery, ...] = (probe.probe,)
    elif arguments.delivery is not None:
        try:
            deliveries = (named_cadence_delivery(planned_all, policy, arguments.delivery),)
        except LandingError as error:
            raise SystemExit(f"named delivery refused: {error}") from None
    else:
        deliveries = plan_landings(ROOT, arguments.selection)
    historical = sum(
        1 for delivery in deliveries if is_historical(manifest_facts(delivery.manifest), policy)
    )
    if historical:
        # V-8 hard deny (S2): refused before any planning output or put.
        raise SystemExit(
            f"FULL_HISTORY_NOT_ENABLED: the selection holds {historical} historical deliveries"
        )
    if not deliveries:
        raise SystemExit(
            "the plan selection is empty: no delivery matches --classes and the bounds"
        )
    earliest = _earliest_landing(deliveries)
    plan_digest = _plan_digest(deliveries)
    total = len(deliveries)

    print("LAND FIXTURES INTO THE V2 RAW ZONE (dev only)")  # noqa: T201
    print("=" * 60)  # noqa: T201
    for delivery in deliveries:
        print(f"  {delivery.fixture}")  # noqa: T201
        for planned in delivery.ordered:
            print(f"      {len(planned.body):>7} B  {planned.key}")  # noqa: T201
    objects = sum(len(delivery.ordered) for delivery in deliveries)
    print(f"  total: {objects} object(s) across {total} deliveries")  # noqa: T201
    print(f"  earliest landing: {earliest.isoformat()}")  # noqa: T201
    if probe is not None:
        report = probe_report(probe, no_twin=arguments.no_twin)
        print(f"PROBE_RETRIEVAL {json.dumps(report, sort_keys=True)}")  # noqa: T201
    print()  # noqa: T201

    if not arguments.dry_run and env_name != DEV_ONLY:
        raise NotDevError(
            f"this job writes fixtures and will only run against {DEV_ONLY!r}, not {env_name!r}."
        )

    execution_id = uuid4().hex
    cancelled = threading.Event()
    previous_signals = _install_cancellation_signals(cancelled) if not arguments.dry_run else {}
    progress = LandFixturesProgress(
        operation_id=arguments.operation_id,
        execution_id=execution_id,
        predecessor_execution_id=arguments.predecessor_execution_id,
        plan_digest=plan_digest,
        stage="start",
        completed=0,
        total=total,
    )
    session = boto3.Session(profile_name=arguments.profile, region_name=arguments.region)
    heartbeat: _Heartbeat | None = None
    try:
        tracing_active = configure_tracing(
            TracingSettings(
                enabled=arguments.trace,
                aws_region=arguments.region,
                environment=env_name,
                sample_ratio=1.0,
            ),
            session=session._session,
        )
        with span(
            LAND_FIXTURES_PROCESS,
            operation_id=arguments.operation_id,
            execution_id=execution_id,
            predecessor_execution_id=arguments.predecessor_execution_id or "none",
            plan_digest=plan_digest,
            stage="start",
            completed=0,
            total=total,
        ) as root_span:
            _lifecycle(
                progress,
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
                tracing=tracing_active,
            )
            identity = session.client("sts").get_caller_identity()
            if str(identity.get("Account", "")) != arguments.expected_account:
                raise PermissionError(
                    "caller account does not match the explicitly expected account"
                )
            cloudformation = session.client("cloudformation")
            ssm = session.client("ssm")
            verify_named_raw_bucket(
                NamedRawResourceRequest(
                    environment=env_name,
                    stack_name=arguments.stack,
                    bucket=arguments.bucket,
                    cloudformation=cloudformation,
                    ssm=ssm,
                )
            )
            if arguments.twin_only and probe is not None:
                return _send_reserved_twin(
                    session, arguments, probe, principal=str(identity.get("Arn", ""))
                )
            if arguments.dry_run:
                progress = LandFixturesProgress(
                    operation_id=arguments.operation_id,
                    execution_id=execution_id,
                    predecessor_execution_id=None,
                    plan_digest=plan_digest,
                    stage="plan",
                    completed=total,
                    total=total,
                )
                root_span.record(stage="plan", completed=total, outcome="completed")
                _lifecycle(
                    progress,
                    phase="progress",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                )
                _lifecycle(
                    progress,
                    phase="terminal",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                )
                print("Dry run. Nothing was written.")  # noqa: T201
                return 0
            instant = now()
            if instant < earliest:
                # Checked after the identity and bucket checks and before the first put, so
                # a refusal is reported like every other one and nothing is written.
                raise LandingError(
                    f"plan retrieved_at {earliest.isoformat()} is in the future; "
                    f"land after {earliest.isoformat()}"
                )
            seal_guard: SealSequenceGuard | None = None
            if any(delivery.delivery_class is DeliveryClass.SEAL for delivery in deliveries):
                _require_settled(arguments, str(identity.get("Account", "")), instant)

                def report_seal(wait: SealWait) -> None:
                    _lifecycle(
                        progress,
                        phase="progress",
                        outcome="sealed",
                        severity=LogSeverity.INFO,
                        seal_fixture=wait.fixture,
                        seal_polls=wait.polls,
                        seal_waited_seconds=round(wait.waited_seconds, 1),
                        seal_publication_id=wait.publication_id,
                    )

                seal_guard = SealSequenceGuard(_seal_current_reader(session, env_name), report_seal)
            print(  # noqa: T201
                f"  apply identity: account={identity.get('Account', '')} "
                f"arn={identity.get('Arn', '')}"
            )
            print(f"  target: s3://{arguments.bucket} ({env_name})")  # noqa: T201

            def report_heartbeat(snapshot: LandFixturesProgress) -> None:
                _lifecycle(
                    snapshot,
                    phase="heartbeat",
                    outcome="unchanged",
                    severity=LogSeverity.DEBUG,
                )

            heartbeat = _Heartbeat(report_heartbeat, progress)

            def report_progress(snapshot: LandFixturesProgress) -> None:
                nonlocal progress
                progress = snapshot
                heartbeat.update(snapshot)
                root_span.record(
                    stage=snapshot.stage,
                    completed=snapshot.completed,
                    total=snapshot.total,
                )
                _lifecycle(
                    snapshot,
                    phase="progress",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                )

            heartbeat.start()
            try:
                count = apply_immutable(
                    ApplyLandFixturesRequest(
                        client=session.client("s3"),
                        bucket=arguments.bucket,
                        deliveries=deliveries,
                        operation_id=arguments.operation_id,
                        execution_id=execution_id,
                        predecessor_execution_id=arguments.predecessor_execution_id,
                        plan_digest=plan_digest,
                        progress=report_progress,
                        cancelled=cancelled.is_set,
                        historical_policy=policy,
                        seal_guard=seal_guard,
                    )
                )
            finally:
                heartbeat.stop()
            progress = LandFixturesProgress(
                operation_id=arguments.operation_id,
                execution_id=execution_id,
                predecessor_execution_id=arguments.predecessor_execution_id,
                plan_digest=plan_digest,
                stage="apply",
                completed=total,
                total=total,
            )
            root_span.record(stage="apply", completed=total, outcome="completed")
            _lifecycle(
                progress,
                phase="terminal",
                outcome="completed",
                severity=LogSeverity.INFO,
            )
            print(  # noqa: T201
                f"Created {count} object(s). Each manifest followed its data; existing keys were "
                "accepted only when byte-identical."
            )
            if probe is not None and not arguments.no_twin:
                # S6 (V-19): the probe retrieval's on_demand twin, decided by the mode.
                twin = send_probe_twin(
                    session,
                    ProbeTwinRequest(
                        environment=env_name,
                        store_stack=str(arguments.graph_store_stack),
                        bucket=arguments.bucket,
                        manifest_key=probe.probe.manifest.manifest_key,
                        principal=str(identity.get("Arn", "")),
                    ),
                )
                print(f"PROBE_TWIN {json.dumps(twin, sort_keys=True)}")  # noqa: T201
                if twin.get("refused") or twin.get("decision") == "REFUSED":
                    return 3
            return 0
    except LandFixturesCancelledError, KeyboardInterrupt:
        _lifecycle(
            progress,
            phase="terminal",
            outcome="cancelled",
            severity=LogSeverity.WARNING,
        )
        print(f"Next predecessor execution ID: {execution_id}")  # noqa: T201
        return 130
    except Exception as error:
        report_error(
            error,
            Report(
                operation=_OPERATION,
                correlation_id=execution_id,
                logger=LOGGER,
            ),
        )
        _lifecycle(
            progress,
            phase="terminal",
            outcome="failed",
            severity=LogSeverity.WARNING,
        )
        return 1
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        _restore_cancellation_signals(previous_signals)
        try:
            flush_metrics()
        finally:
            flush_traces()


def _send_reserved_twin(  # noqa: PLR0913 -- the session, inputs, plan and verified caller
    session: boto3.Session, arguments: _Arguments, probe: ProbeRetrieval, *, principal: str
) -> int:
    """D7 launch child: verify the reserved key's one immutable version, then send its twin."""
    manifest = probe.probe.marker
    verify_reserved_probe(session, arguments.bucket, probe.probe)
    twin = send_probe_twin(
        session,
        ProbeTwinRequest(
            environment=arguments.environment,
            store_stack=str(arguments.graph_store_stack),
            bucket=arguments.bucket,
            manifest_key=manifest.key,
            principal=principal,
        ),
    )
    print(f"PROBE_TWIN {json.dumps(twin, sort_keys=True)}")  # noqa: T201
    return 3 if twin.get("refused") or twin.get("decision") == "REFUSED" else 0


if __name__ == "__main__":
    sys.exit(main())
