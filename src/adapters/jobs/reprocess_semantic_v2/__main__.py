# SPDX-License-Identifier: MIT-0
"""Dry-run-first CLI for exact-version, non-erasing Semantic V2 reprocessing."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import FrameType
from uuid import uuid4

import boto3
from aws_lambda_powertools import Logger

from adapters.config import TracingSettings
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import checksum_of
from adapters.jobs.execution_graph.resolution import resolve_graph_table
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarTooling
from adapters.jobs.reprocess_semantic_v2.acceptance import (
    ReprocessAcceptanceRequest,
    attest_reprocess,
)
from adapters.jobs.reprocess_semantic_v2.job import (
    ApplyReprocessRequest,
    PlanReprocessRequest,
    ReprocessCancelledError,
    ReprocessProgress,
    apply_reprocess,
    plan_reprocess,
)
from adapters.jobs.reprocess_semantic_v2.resources import (
    NamedRawResourceRequest,
    resolve_named_semantic_resources,
    verify_named_raw_bucket,
)
from adapters.jobs.schema_generation.publisher_graph import load_publication_graph
from adapters.outbound.athena.semantic_storage import AthenaSemanticTarget
from adapters.outbound.audience_segment_providers import load_segment_providers
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.semantic_state import (
    DynamoAttemptStore,
    DynamoLeaseStore,
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
)
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.s3.versioned_raw import S3VersionedRawReader
from adapters.outbound.semantic_storage import (
    ReplicatedSemanticStorageRequest,
    replicated_semantic_storage,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.table_dq import load_runtime_table_rules
from adapters.outbound.telemetry.bootstrap import configure_tracing, flush_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    REPROCESS_ACCEPTANCE,
    REPROCESS_APPLY,
    REPROCESS_PLAN,
    REPROCESS_PROCESS,
    span,
    trace_boundary,
)
from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog
from adapters.producer.packs import discard_policies as discard_policies_for
from adapters.producer.packs import installed_governance as installed_governance_for
from adapters.producer.packs import load_query_packs
from domain.data_contracts.contract import DataContract
from domain.operations.execution_graph import AuthorityMode
from domain.semantics.publication import CanonicalDatasetKey
from domain.semantics.reprocessing import (
    ContractArtifact,
    ReprocessOperationId,
    ReprocessPlan,
    ReprocessRequest,
)
from domain.shared_kernel.errors import REPROCESS_REQUIRES_GRAPH_RUN, ValidationError
from domain.shared_kernel.values import DateRange

ROOT = pathlib.Path(__file__).resolve().parents[4]
LOGGER = Logger(service="adlytics-reprocess-semantic-v2", level="DEBUG")
_OPERATION = REPROCESS_PROCESS
_COMPONENT = "reprocess_semantic_v2.operator"
_HEARTBEAT_INTERVAL_SECONDS = 30.0


class ReprocessHeartbeat:
    """Background heartbeat that repeats only the last completed progress snapshot."""

    def __init__(
        self,
        reporter: Callable[[ReprocessProgress], None],
        initial: ReprocessProgress,
        *,
        interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reprocess heartbeat interval must be positive.")
        self._reporter = reporter
        self._progress = initial
        self._interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, progress: ReprocessProgress) -> None:
        """Replace the snapshot only after the caller completed a durable stage."""
        with self._lock:
            if progress.completed < self._progress.completed:
                raise ValueError("reprocess heartbeat progress cannot move backwards.")
            self._progress = progress

    def emit_once(self) -> None:
        """Emit the current snapshot; directly testable without sleeping."""
        with self._lock:
            progress = self._progress
        self._reporter(progress)

    def start(self) -> None:
        """Start one daemon reporter thread."""
        if self._thread is not None:
            raise RuntimeError("reprocess heartbeat already started.")
        self._thread = threading.Thread(
            target=self._run,
            name="reprocess-semantic-v2-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop and join the reporter thread on every terminal path."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self.emit_once()


def _account(value: str) -> str:
    normalized = _non_blank(value).strip()
    if re.fullmatch(r"\d{12}", normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def _lineage_details(  # noqa: PLR0913
    *,
    execution_id: str,
    predecessor_execution_id: str | None,
    plan_digest: str | None,
    stage: str,
    completed: int,
    total: int,
    **extra: object,
) -> dict[str, object]:
    details: dict[str, object] = {
        "execution_id": execution_id,
        "stage": stage,
        "completed": completed,
        "total": total,
        **extra,
    }
    if predecessor_execution_id is not None:
        details["predecessor_execution_id"] = predecessor_execution_id
    if plan_digest is not None:
        details["plan_digest"] = plan_digest
    return details


def _lifecycle(  # noqa: PLR0913
    operation_id: str,
    *,
    phase: str,
    outcome: str,
    severity: LogSeverity,
    details: dict[str, object],
    description: str = "",
) -> None:
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase=phase,
            outcome=outcome,
            severity=severity,
            description=description,
            details={"operation_id": operation_id, **details},
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


def _restore_cancellation_signals(
    previous: dict[signal.Signals, _SignalHandler],
) -> None:
    for selected, handler in previous.items():
        if handler is not None:
            signal.signal(selected, handler)


def _checkpoint(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise ReprocessCancelledError("reprocess cancellation requested")


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value


def _profile(value: str) -> str:
    normalized = _non_blank(value).strip()
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    """Build an explicit-scope parser with dry-run as the only default."""
    parser = argparse.ArgumentParser(
        description="Plan or apply append-only Semantic V2 replay from retained raw versions."
    )
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--env", required=True)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--stack", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--window-start", required=True, type=date.fromisoformat)
    parser.add_argument("--window-end", required=True, type=date.fromisoformat)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--contract", required=True, action="append", dest="contracts")
    parser.add_argument(
        "--retrieved-token",
        type=_non_blank,
        action="append",
        dest="retrieved_tokens",
        help=(
            "Repeatable exact retained path token YYYYMMDDTHHMMSSZ; "
            "included in the reviewed plan digest."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--graph-store-stack",
        type=_non_blank,
        help=(
            "Required with --apply: the store stack that owns the execution-graph table whose "
            "mode the apply reads strongly before any write (it refuses in GRAPH)."
        ),
    )
    parser.add_argument("--expected-plan-digest", type=_non_blank)
    parser.add_argument("--predecessor-execution-id", type=_non_blank)
    return parser


def _report(plan: object) -> None:
    if not isinstance(plan, ReprocessPlan):
        raise TypeError("reprocess report requires a ReprocessPlan.")
    print("SEMANTIC V2 REPROCESS PLAN")  # noqa: T201
    print("=" * 72)  # noqa: T201
    print(f"  operation: {plan.request.operation_id.value}")  # noqa: T201
    print(f"  digest:    {plan.digest}")  # noqa: T201
    for prefix in plan.prefixes:
        print(f"  LIST VERSIONS s3://{plan.request.bucket}/{prefix}*")  # noqa: T201
    for delivery in plan.deliveries:
        print(  # noqa: T201
            f"  manifest {delivery.manifest_key} version={delivery.manifest_version_id} "
            f"checksum={delivery.manifest_checksum}"
        )
        print(  # noqa: T201
            f"  object   {delivery.object.key} version={delivery.object.version_id} "
            f"bytes={delivery.object.actual_byte_count} "
            f"checksum={delivery.object.actual_checksum}"
        )


def _require_apply_pairs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse an apply-only flag without --apply, and --apply without its required inputs."""
    if args.apply and args.expected_plan_digest is None:
        parser.error("--apply requires --expected-plan-digest")
    if args.expected_plan_digest is not None and not args.apply:
        parser.error("--expected-plan-digest requires --apply")
    if args.predecessor_execution_id is not None and not args.apply:
        parser.error("--predecessor-execution-id requires --apply")
    if args.apply and args.graph_store_stack is None:
        parser.error("--apply requires --graph-store-stack")


def require_reprocess_mode(mode: AuthorityMode) -> None:
    """D22: refuse an apply in `GRAPH`, where only the graph may advance servability.

    The apply commits with `inline_projection=True`, which advances servability at commit; in
    `GRAPH` that would be a reader-visible move outside `publish`. A graph-native reprocess is an
    `on_demand` `data_correction` run request (backlogged).
    """
    if mode is AuthorityMode.GRAPH:
        raise ValidationError(
            REPROCESS_REQUIRES_GRAPH_RUN,
            "Reprocess apply is refused in GRAPH; request an on_demand data_correction run.",
            context={"mode": mode.value},
        )


def _apply_mode(session: boto3.Session, environment: str, graph_store_stack: str) -> AuthorityMode:
    """Read the authority mode once, strongly, through the named store stack's own table."""
    table = resolve_graph_table(session, environment, graph_store_stack)
    return DynamoExecutionGraphStore(table, session.client("dynamodb")).read_mode().mode


def _require_expected_plan(plan: ReprocessPlan, expected_digest: str) -> None:
    if not plan.deliveries:
        raise ValueError("reprocess apply refuses an empty plan.")
    if expected_digest != plan.digest:
        raise ValueError("reprocess apply plan digest differs from the reviewed dry run.")


@dataclass(frozen=True, slots=True)
class _InstalledContracts:
    contracts: tuple[DataContract, ...]
    artifacts: tuple[ContractArtifact, ...]


def _installed_contracts() -> _InstalledContracts:
    loaded = load_contracts(ROOT)
    return _InstalledContracts(
        contracts=tuple(contract for _, contract in loaded),
        artifacts=tuple(
            ContractArtifact(contract.contract_id, checksum_of(path.read_bytes()))
            for path, contract in loaded
        ),
    )


def _semantic_authority_digest() -> str:
    """Bind the exact local code, contracts, generated authorities, and dependency lock."""
    files = [
        path
        for directory in (ROOT / "src", ROOT / "contracts")
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith(".")
    ]
    files.extend(ROOT / name for name in ("pyproject.toml", "uv.lock"))
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(ROOT).as_posix().encode()
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return f"sha256:{digest.hexdigest()}"


@trace_boundary(
    REPROCESS_PROCESS,
    REPROCESS_PLAN,
    REPROCESS_APPLY,
    REPROCESS_ACCEPTANCE,
)
def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915
    """Verify identity and named resources, then plan or apply exact retained versions."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _require_apply_pairs(parser, args)
    expected_plan_digest: str | None = args.expected_plan_digest

    request = ReprocessRequest(
        environment=args.env,
        stack_name=args.stack,
        bucket=args.bucket,
        contracts=tuple(args.contracts),
        window=DateRange(args.window_start, args.window_end),
        operation_id=ReprocessOperationId(args.operation_id),
        retrieved_tokens=tuple(args.retrieved_tokens or ()),
    )
    execution_id = uuid4().hex
    predecessor_execution_id: str | None = args.predecessor_execution_id
    plan_digest: str | None = None
    last_stage = "start"
    last_completed = 0
    last_total = 0
    cancelled = threading.Event()
    tracing_settings = TracingSettings(
        enabled=args.trace,
        aws_region=args.region,
        environment=args.env,
        sample_ratio=1.0,
    )
    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    previous_signals = _install_cancellation_signals(cancelled)
    try:
        # Bootstrap precedes the first SDK client so the identity check itself is instrumented.
        tracing_active = configure_tracing(tracing_settings, session=session._session)
        identity = session.client("sts").get_caller_identity()
        if str(identity.get("Account", "")) != args.expected_account:
            raise PermissionError("caller account does not match the explicitly expected account")
        _checkpoint(cancelled)
        with span(
            REPROCESS_PROCESS,
            operation_id=request.operation_id.value,
            execution_id=execution_id,
            predecessor_execution_id=predecessor_execution_id or "none",
            plan_digest="unbound",
            stage="start",
            completed=0,
            total=0,
        ) as root_span:
            _lifecycle(
                request.operation_id.value,
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
                details=_lineage_details(
                    execution_id=execution_id,
                    predecessor_execution_id=predecessor_execution_id,
                    plan_digest=None,
                    stage="start",
                    completed=0,
                    total=0,
                    tracing=tracing_active,
                ),
            )
            _lifecycle(
                request.operation_id.value,
                phase="identity",
                outcome="verified",
                severity=LogSeverity.DEBUG,
                details=_lineage_details(
                    execution_id=execution_id,
                    predecessor_execution_id=predecessor_execution_id,
                    plan_digest=None,
                    stage="identity",
                    completed=0,
                    total=0,
                ),
            )

            cloudformation = session.client("cloudformation")
            ssm = session.client("ssm")
            s3 = session.client("s3")
            verify_named_raw_bucket(
                NamedRawResourceRequest(
                    environment=args.env,
                    stack_name=args.stack,
                    bucket=args.bucket,
                    cloudformation=cloudformation,
                    ssm=ssm,
                )
            )
            installed = _installed_contracts()
            semantic_authority_digest = _semantic_authority_digest()
            packs = load_source_packs(ROOT)
            installed_governance = installed_governance_for(load_query_packs(ROOT))
            raw = S3VersionedRawReader(args.bucket, s3)
            with span(
                REPROCESS_PLAN,
                operation_id=request.operation_id.value,
                execution_id=execution_id,
                predecessor_execution_id=predecessor_execution_id or "none",
                plan_digest="unbound",
                stage="plan",
                completed=0,
                total=0,
            ) as plan_span:
                plan = plan_reprocess(
                    PlanReprocessRequest(
                        request=request,
                        contracts=installed.contracts,
                        packs=packs,
                        raw=raw,
                        installed_governance=installed_governance,
                        contract_artifacts=installed.artifacts,
                        semantic_authority_digest=semantic_authority_digest,
                    )
                )
                plan_digest = plan.digest
                delivery_count = len(plan.deliveries)
                plan_span.record(
                    plan_digest=plan.digest,
                    completed=delivery_count,
                    total=delivery_count,
                )
            last_stage = "plan"
            last_completed = delivery_count if not args.apply else 0
            last_total = delivery_count if not args.apply else delivery_count * 2
            root_span.record(
                plan_digest=plan.digest,
                stage=last_stage,
                completed=last_completed,
                total=last_total,
            )
            _lifecycle(
                request.operation_id.value,
                phase="progress",
                outcome="completed",
                severity=LogSeverity.INFO,
                details=_lineage_details(
                    execution_id=execution_id,
                    predecessor_execution_id=predecessor_execution_id,
                    plan_digest=plan.digest,
                    stage=last_stage,
                    completed=last_completed,
                    total=last_total,
                ),
            )
            _report(plan)
            _checkpoint(cancelled)
            if not args.apply:
                _lifecycle(
                    request.operation_id.value,
                    phase="terminal",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                    details=_lineage_details(
                        execution_id=execution_id,
                        predecessor_execution_id=None,
                        plan_digest=plan.digest,
                        stage="plan",
                        completed=delivery_count,
                        total=delivery_count,
                    ),
                )
                return 0

            if expected_plan_digest is None:  # pragma: no cover - parser guard narrows apply mode
                raise RuntimeError("reprocess apply requires its reviewed plan digest.")
            _require_expected_plan(plan, expected_plan_digest)
            # S6 (D22): one strong mode read, before any write or semantic resource is resolved.
            require_reprocess_mode(_apply_mode(session, args.env, str(args.graph_store_stack)))
            _checkpoint(cancelled)
            semantic_resources = resolve_named_semantic_resources(args.env, cloudformation, ssm)
            dynamodb = session.client("dynamodb")
            athena = session.client("athena")
            rules = load_runtime_table_rules(ROOT / "contracts/generated/dq-rulesets.json")
            storage = replicated_semantic_storage(
                ReplicatedSemanticStorageRequest(
                    bucket=semantic_resources["bucket"],
                    target=AthenaSemanticTarget(
                        batch_workgroup=semantic_resources["bake_workgroup"],
                        source_database=semantic_resources["source_database"],
                        candidate_database=semantic_resources["candidate_database"],
                        current_database=semantic_resources["current_database"],
                    ),
                    catalog=load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json"),
                    s3=s3,
                    athena=athena,
                )
            )
            attempts = DynamoAttemptStore(semantic_resources["attempts"], dynamodb)
            leases = DynamoLeaseStore(semantic_resources["leases"], dynamodb)
            publications = DynamoPublicationStore(
                semantic_resources["publications"],
                semantic_resources["leases"],
                DynamoPublicationStoreAccess(
                    attempts_table=semantic_resources["attempts"],
                    client=dynamodb,
                ),
            )
            semantic = SemanticSidecarTooling(
                source_writer=storage.source,
                source_reader=storage.source,
                canonical_writer=storage.canonical,
                attempts=attempts,
                leases=leases,
                publications=publications,
                packs=packs,
                bridge=load_commerce_identity_bridge(ROOT),
                graph=load_publication_graph(
                    ROOT,
                    frozenset(CanonicalDatasetKey(table) for table in rules),
                ),
                table_rules=rules,
                columns=load_registry(ROOT),
                catalog=load_catalog(ROOT),
                identity_evidence=storage.identity_evidence,
                taxonomy=load_media_taxonomy(ROOT),
                segment_providers=load_segment_providers(ROOT),
                canonical_reader=storage.canonical,
            )
            applied_at = datetime.now(UTC)
            apply_total = delivery_count * 2
            initial_progress = ReprocessProgress(
                operation_id=request.operation_id.value,
                execution_id=execution_id,
                predecessor_execution_id=predecessor_execution_id,
                plan_digest=plan.digest,
                stage="plan",
                completed=0,
                total=apply_total,
            )

            def report_heartbeat(progress: ReprocessProgress) -> None:
                _lifecycle(
                    progress.operation_id,
                    phase="heartbeat",
                    outcome="unchanged",
                    severity=LogSeverity.DEBUG,
                    details=_lineage_details(
                        execution_id=progress.execution_id,
                        predecessor_execution_id=progress.predecessor_execution_id,
                        plan_digest=progress.plan_digest,
                        stage=progress.stage,
                        completed=progress.completed,
                        total=progress.total,
                        contract_ids=progress.contract_ids,
                    ),
                )

            heartbeat = ReprocessHeartbeat(report_heartbeat, initial_progress)

            def report_progress(progress: ReprocessProgress) -> None:
                nonlocal last_completed, last_stage, last_total
                heartbeat.update(progress)
                last_stage = progress.stage
                last_completed = progress.completed
                last_total = progress.total
                _lifecycle(
                    progress.operation_id,
                    phase="progress",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                    details=_lineage_details(
                        execution_id=progress.execution_id,
                        predecessor_execution_id=progress.predecessor_execution_id,
                        plan_digest=progress.plan_digest,
                        stage=progress.stage,
                        completed=progress.completed,
                        total=progress.total,
                        contract_ids=progress.contract_ids,
                    ),
                )

            with span(
                REPROCESS_APPLY,
                operation_id=request.operation_id.value,
                execution_id=execution_id,
                predecessor_execution_id=predecessor_execution_id or "none",
                plan_digest=plan.digest,
                stage="apply",
                completed=0,
                total=apply_total,
            ) as apply_span:
                heartbeat.start()
                try:
                    outcomes = apply_reprocess(
                        ApplyReprocessRequest(
                            plan=plan,
                            contracts=installed.contracts,
                            registry=load_registry(ROOT),
                            raw=raw,
                            semantic=semantic,
                            execution_id=execution_id,
                            applied_at=applied_at,
                            predecessor_execution_id=predecessor_execution_id,
                            installed_governance=installed_governance,
                            discard_policies=discard_policies_for(load_query_packs(ROOT)),
                            contract_artifacts=installed.artifacts,
                            semantic_authority_digest=semantic_authority_digest,
                            progress=report_progress,
                            cancelled=cancelled.is_set,
                        )
                    )
                finally:
                    heartbeat.stop()
                apply_span.record(completed=apply_total)
            _checkpoint(cancelled)
            with span(
                REPROCESS_ACCEPTANCE,
                operation_id=request.operation_id.value,
                execution_id=execution_id,
                predecessor_execution_id=predecessor_execution_id or "none",
                plan_digest=plan.digest,
                stage="acceptance",
                completed=apply_total,
                total=apply_total,
            ) as acceptance_span:
                acceptance = attest_reprocess(
                    ReprocessAcceptanceRequest(
                        outcomes=outcomes,
                        storage=storage,
                        attempts=attempts,
                        publications=publications,
                    )
                )
                acceptance_span.record(accepted=acceptance.accepted)
            _checkpoint(cancelled)
            if not acceptance.accepted:
                _lifecycle(
                    request.operation_id.value,
                    phase="terminal",
                    outcome="failed",
                    severity=LogSeverity.WARNING,
                    description=acceptance.reason,
                    details=_lineage_details(
                        execution_id=execution_id,
                        predecessor_execution_id=predecessor_execution_id,
                        plan_digest=plan.digest,
                        stage="acceptance",
                        completed=apply_total,
                        total=apply_total,
                    ),
                )
                return 1
            root_span.record(stage="acceptance", completed=apply_total, total=apply_total)
            _lifecycle(
                request.operation_id.value,
                phase="terminal",
                outcome="completed",
                severity=LogSeverity.INFO,
                details=_lineage_details(
                    execution_id=execution_id,
                    predecessor_execution_id=predecessor_execution_id,
                    plan_digest=plan.digest,
                    stage="acceptance",
                    completed=apply_total,
                    total=apply_total,
                ),
            )
            return 0
    except ReprocessCancelledError, KeyboardInterrupt:
        _lifecycle(
            request.operation_id.value,
            phase="terminal",
            outcome="cancelled",
            severity=LogSeverity.WARNING,
            details=_lineage_details(
                execution_id=execution_id,
                predecessor_execution_id=predecessor_execution_id,
                plan_digest=plan_digest,
                stage=last_stage,
                completed=last_completed,
                total=last_total,
            ),
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
            request.operation_id.value,
            phase="terminal",
            outcome="failed",
            severity=LogSeverity.WARNING,
            details=_lineage_details(
                execution_id=execution_id,
                predecessor_execution_id=predecessor_execution_id,
                plan_digest=plan_digest,
                stage=last_stage,
                completed=last_completed,
                total=last_total,
            ),
        )
        return 1
    finally:
        _restore_cancellation_signals(previous_signals)
        try:
            flush_metrics()
        finally:
            flush_traces()


if __name__ == "__main__":
    sys.exit(main())
