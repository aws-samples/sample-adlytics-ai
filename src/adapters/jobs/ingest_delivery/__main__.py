# SPDX-License-Identifier: MIT-0
"""Load one landed delivery, or every delivery in a dev raw zone.

    uv run python -m adapters.jobs.ingest_delivery --profile <profile> --dry-run
    uv run python -m adapters.jobs.ingest_delivery --profile <profile> --env dev
    uv run python -m adapters.jobs.ingest_delivery --profile <profile> \
      --key <manifest key> --version-id <manifest VersionId>

Without `--key` it finds every current `manifest.json`, freezes each marker's exact VersionId, and
loads those selected versions in lexicographic key order. That ordering is a convenience for a bulk
dev load and nothing depends on it: the watermark decision is what makes an out-of-order arrival
safe, and the restatement pair is landed specifically to prove it.

`--dry-run` lists what would be loaded and **writes** nothing. Unlike the landing job's dry run
it still needs credentials, because what it would load is discovered by listing the bucket
rather than computed from files on disk — there is nothing to plan against locally.

**Every target value comes from the SSM parameters the stacks publish**, never from `ADL_*`
environment variables. Those exist inside a Lambda; a developer at a terminal has none of them,
and a full `Settings` would demand Cognito values a load never uses. Reading the parameters is
itself the assertion that the stacks owning the bucket and the tables are deployed.

The profile is a required argument with no default, and every AWS client comes from that one
explicit session.
"""

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
from datetime import UTC, datetime
from types import FrameType
from typing import TYPE_CHECKING
from uuid import uuid4

import boto3
from aws_lambda_powertools import Logger

from adapters.config import TracingSettings
from adapters.inbound.run_requests.service import AdmissionDependencies, admit_run_request
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.execution_graph.resolution import resolve_graph_table
from adapters.jobs.ingest_delivery.authority_gate import (
    GatePath,
    OperatorGate,
    decide_gate,
    record_shadow,
)
from adapters.jobs.ingest_delivery.landing import (
    LoadedDelivery,
    ManifestInput,
    v2_operation_id,
)
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarTooling
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
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.governed_materialization import GovernedMaterializationPort
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.s3.run_request_manifests import S3RunRequestManifests
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
from adapters.outbound.telemetry.tracing import INGEST_OPERATOR_PROCESS, span, trace_boundary
from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog
from adapters.producer.packs import discard_policies, load_query_packs
from adapters.producer.packs import installed_governance as installed_governance_for
from domain.curation.ingestion import DiscardPolicy
from domain.curation.landing import (
    MANIFEST_NAME,
    LandingError,
    contracts_for,
    require_installed_governance,
)
from domain.semantics.publication import CanonicalDatasetKey
from domain.shared_kernel.errors import ErrorCode, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_athena.client import AthenaClient
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_s3.client import S3Client

    from domain.data_contracts.contract import DataContract
    from domain.data_contracts.validation import ColumnRegistry
    from domain.shared_kernel.provenance import ProducerGovernance


ROOT = pathlib.Path(__file__).resolve().parents[4]
DEV_ONLY = "dev"
LOGGER = Logger(service="adlytics-ingest-delivery", level="DEBUG")
_OPERATION = INGEST_OPERATOR_PROCESS
_COMPONENT = "ingest_delivery.operator"
_HEARTBEAT_INTERVAL_SECONDS = 30.0
DELIVERY_REFUSED = ErrorCode("INGEST_OPERATOR", 1)


class IngestCancelledError(Exception):
    """Operator ingestion stopped at a safe delivery boundary."""


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
    manifest: ManifestInput | None
    predecessor_execution_id: str | None
    dry_run: bool
    graph_store_stack: str = ""
    """S6 (V-19): the store stack that must own the execution-graph table the gate reads."""


@dataclass(frozen=True, slots=True)
class _Progress:
    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str | None
    stage: str
    completed: int
    total: int


class _Heartbeat:
    """Repeat only the latest durable ingest progress snapshot."""

    def __init__(
        self,
        reporter: Callable[[_Progress], None],
        initial: _Progress,
        *,
        interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._reporter = reporter
        self._progress = initial
        self._interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, progress: _Progress) -> None:
        with self._lock:
            if progress.completed < self._progress.completed:
                raise ValueError("ingest progress cannot move backwards.")
            self._progress = progress

    def emit_once(self) -> None:
        with self._lock:
            progress = self._progress
        self._reporter(progress)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="ingest-delivery-heartbeat",
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
    """Reject explicitly blank option values instead of falling back to broad defaults."""
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value


def _profile(value: str) -> str:
    normalized = _non_blank(value).strip()
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def _account(value: str) -> str:
    normalized = _non_blank(value).strip()
    if re.fullmatch(r"\d{12}", normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def _operator_gate(session: boto3.Session, arguments: _Arguments, principal: str) -> OperatorGate:
    """Resolve the graph table through its stack and compose the mode read and admission."""
    table = resolve_graph_table(session, arguments.environment, arguments.graph_store_stack)
    store = DynamoExecutionGraphStore(table, session.client("dynamodb"))
    s3 = session.client("s3")
    resources = resolve_named_semantic_resources(
        arguments.environment, session.client("cloudformation"), session.client("ssm")
    )
    reader = Stage1WriterAuthorityReader(
        resources["publications"], resources["leases"], session.client("dynamodb")
    )
    deps = AdmissionDependencies(
        store=store,
        manifests=S3RunRequestManifests(arguments.bucket, s3),
        now=lambda: datetime.now(UTC),
        legacy=S3CoordinatorLegacyHead(arguments.bucket, s3, reader),
    )
    return OperatorGate(
        mode=lambda: store.read_mode().mode,
        admit=lambda request: admit_run_request(request, deps),
        principal=principal,
        operation_id=arguments.operation_id,
    )


def _report_shadow_failure(execution_id: str) -> Callable[[Exception], None]:
    def report(error: Exception) -> None:
        report_error(
            error,
            Report(operation="ingest.shadow_recording", correlation_id=execution_id, logger=LOGGER),
        )

    return report


def _parse_args(argv: tuple[str, ...]) -> _Arguments:
    """Parse one V2-only invocation with explicit identity and named targets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--env", required=True, type=_non_blank)
    parser.add_argument("--operation-id", required=True, type=_non_blank)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--stack", required=True, type=_non_blank)
    parser.add_argument("--bucket", required=True, type=_non_blank)
    parser.add_argument("--key", type=_non_blank)
    parser.add_argument("--version-id", type=_non_blank)
    parser.add_argument("--predecessor-execution-id", type=_non_blank)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--graph-store-stack", required=True, type=_non_blank)
    parsed = parser.parse_args(argv)
    if (parsed.key is None) != (parsed.version_id is None):
        parser.error("--key and --version-id must be supplied together")
    if parsed.predecessor_execution_id is not None and parsed.key is None:
        parser.error("--predecessor-execution-id requires an explicit --key and --version-id")
    if parsed.predecessor_execution_id is not None and parsed.dry_run:
        parser.error("--predecessor-execution-id cannot be used with --dry-run")
    manifest = (
        None if parsed.key is None else ManifestInput(str(parsed.key), str(parsed.version_id))
    )
    return _Arguments(
        profile=str(parsed.profile),
        region=str(parsed.region),
        expected_account=str(parsed.expected_account),
        environment=str(parsed.env),
        operation_id=str(parsed.operation_id),
        trace=bool(parsed.trace),
        stack=str(parsed.stack),
        bucket=str(parsed.bucket),
        manifest=manifest,
        predecessor_execution_id=(
            None
            if parsed.predecessor_execution_id is None
            else str(parsed.predecessor_execution_id)
        ),
        dry_run=bool(parsed.dry_run),
        graph_store_stack=str(parsed.graph_store_stack),
    )


def _plan_digest(manifests: tuple[ManifestInput, ...]) -> str:
    identities = sorted((item.key, item.version_id) for item in manifests)
    payload = json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _details(progress: _Progress, **extra: object) -> dict[str, object]:
    details: dict[str, object] = {
        "operation_id": progress.operation_id,
        "execution_id": progress.execution_id,
        "predecessor_execution_id": progress.predecessor_execution_id or "none",
        "plan_digest": progress.plan_digest or "unbound",
        "stage": progress.stage,
        "completed": progress.completed,
        "total": progress.total,
        **extra,
    }
    return details


def _lifecycle(  # noqa: PLR0913
    progress: _Progress,
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


def _checkpoint(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise IngestCancelledError("ingest cancellation requested")


def _semantic_names(resources: dict[str, str]) -> dict[str, str]:
    return {
        "raw_bucket": "",
        "semantic_bucket": resources["bucket"],
        "bake_workgroup": resources["bake_workgroup"],
        "source_database": resources["source_database"],
        "candidate_database": resources["candidate_database"],
        "current_database": resources["current_database"],
        "attempts_table": resources["attempts"],
        "leases_table": resources["leases"],
        "publications_table": resources["publications"],
    }


def _manifest_inputs(client: S3Client, bucket: str) -> tuple[ManifestInput, ...]:
    """Discover current markers and freeze each exact VersionId before publication."""
    keys: list[str] = []
    token = ""
    while True:
        page = (
            client.list_objects_v2(Bucket=bucket)
            if not token
            else client.list_objects_v2(Bucket=bucket, ContinuationToken=token)
        )
        # `Key` is NotRequired in the SDK's own typing, so it is fetched with a default rather
        # than indexed.
        keys.extend(
            found
            for found in (str(item.get("Key", "")) for item in page.get("Contents", []))
            if found.endswith(f"/{MANIFEST_NAME}")
        )
        token = str(page.get("NextContinuationToken", ""))
        if not token:
            break

    selected: list[ManifestInput] = []
    for key in sorted(keys):
        version_id = str(client.head_object(Bucket=bucket, Key=key).get("VersionId", ""))
        if not version_id.strip():
            raise LandingError(f"s3://{bucket}/{key} has no current VersionId to select.")
        selected.append(ManifestInput(key, version_id))
    return tuple(selected)


def _writes_a_dimension(
    delivery: LoadedDelivery,
    contracts: tuple[DataContract, ...],
    installed_governance: tuple[ProducerGovernance, ...],
) -> bool:
    """Say whether this delivery feeds a dimension rather than a fact."""
    require_installed_governance(delivery.manifest, installed_governance)
    resolved = contracts_for(delivery.manifest, contracts)
    return any(not contract.targets_a_fact for contract in resolved.contracts)


@dataclass(frozen=True, slots=True)
class _SemanticToolingRequest:
    resolved: dict[str, str]
    s3: S3Client
    dynamodb: DynamoDBClient
    athena: AthenaClient


def _semantic_tooling(request: _SemanticToolingRequest) -> SemanticSidecarTooling:
    """Compose the V2-only publisher."""
    resolved = request.resolved
    rules = load_runtime_table_rules(ROOT / "contracts/generated/dq-rulesets.json")
    graph = load_publication_graph(
        ROOT,
        frozenset(CanonicalDatasetKey(table) for table in rules),
    )
    storage = replicated_semantic_storage(
        ReplicatedSemanticStorageRequest(
            bucket=resolved["semantic_bucket"],
            target=AthenaSemanticTarget(
                batch_workgroup=resolved["bake_workgroup"],
                source_database=resolved["source_database"],
                candidate_database=resolved["candidate_database"],
                current_database=resolved["current_database"],
            ),
            catalog=load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json"),
            s3=request.s3,
            athena=request.athena,
        )
    )
    return SemanticSidecarTooling(
        source_writer=storage.source,
        source_reader=storage.source,
        canonical_writer=storage.canonical,
        attempts=DynamoAttemptStore(resolved["attempts_table"], request.dynamodb),
        leases=DynamoLeaseStore(resolved["leases_table"], request.dynamodb),
        publications=DynamoPublicationStore(
            resolved["publications_table"],
            resolved["leases_table"],
            DynamoPublicationStoreAccess(
                attempts_table=resolved["attempts_table"],
                client=request.dynamodb,
            ),
        ),
        packs=load_source_packs(ROOT),
        bridge=load_commerce_identity_bridge(ROOT),
        graph=graph,
        table_rules=rules,
        columns=load_registry(ROOT),
        catalog=load_catalog(ROOT),
        identity_evidence=storage.identity_evidence,
        taxonomy=load_media_taxonomy(ROOT),
        segment_providers=load_segment_providers(ROOT),
        canonical_reader=storage.canonical,
    )


@dataclass(frozen=True, slots=True)
class _V2Batch:
    resolved: dict[str, str]
    s3: S3Client
    dynamodb: DynamoDBClient
    athena: AthenaClient
    fetched: tuple[tuple[bool, str, LoadedDelivery], ...]
    columns: ColumnRegistry
    contracts: tuple[DataContract, ...]
    installed_governance: tuple[ProducerGovernance, ...]
    installed_discard_policies: tuple[DiscardPolicy, ...]
    operation_id: str
    execution_id: str
    predecessor_execution_id: str | None
    plan_digest: str
    completed_before_load: int
    total: int
    progress: Callable[[_Progress], None]
    cancelled: Callable[[], bool]


def _ingest_v2(batch: _V2Batch) -> None:
    """Publish a fetched batch through Semantic V2 and report durable delivery decisions."""
    semantic = _semantic_tooling(
        _SemanticToolingRequest(
            resolved=batch.resolved,
            s3=batch.s3,
            dynamodb=batch.dynamodb,
            athena=batch.athena,
        )
    )
    completed = batch.completed_before_load
    for _is_dimension, _key, delivery in batch.fetched:
        if batch.cancelled():
            raise IngestCancelledError("ingest cancellation requested")
        now = datetime.now(tz=UTC)
        delivery_operation_id = v2_operation_id(delivery)
        print(  # noqa: T201
            f"  execution_id={batch.execution_id} "
            f"predecessor_execution_id={batch.predecessor_execution_id or 'none'} "
            f"manifest={delivery.landed_ref.manifest_key}@"
            f"{delivery.landed_ref.manifest_version}"
        )
        results = ingest_semantic_delivery(
            SemanticIngestionRequest(
                delivery=delivery,
                contracts=batch.contracts,
                columns=batch.columns,
                now=now,
                operation_id=delivery_operation_id,
                execution_id=batch.execution_id,
                predecessor_execution_id=batch.predecessor_execution_id,
                plan_digest=batch.plan_digest,
                installed_governance=batch.installed_governance,
                discard_policies=batch.installed_discard_policies,
            ),
            semantic,
        )
        for result in results:
            print(  # noqa: T201
                f"  {result.outcome.value.upper():<11} {result.contract_id:<30} "
                f"{len(result.publications):>4} publication(s)  {result.detail}"
            )
        completed += 1
        batch.progress(
            _Progress(
                operation_id=batch.operation_id,
                execution_id=batch.execution_id,
                predecessor_execution_id=batch.predecessor_execution_id,
                plan_digest=batch.plan_digest,
                stage="apply",
                completed=completed,
                total=batch.total,
            )
        )


@trace_boundary(INGEST_OPERATOR_PROCESS)
def main(argv: tuple[str, ...] | None = None) -> int:  # noqa: PLR0912, PLR0915
    """Load immutable V2 deliveries through one explicit, traced operator scope."""
    arguments = _parse_args(tuple(sys.argv[1:]) if argv is None else argv)
    env_name = arguments.environment

    print("INGEST V2 LANDED DELIVERIES (dev only)")  # noqa: T201
    print("=" * 72)  # noqa: T201
    if env_name != DEV_ONLY:
        raise SystemExit(f"this job will only run against {DEV_ONLY!r}, not {env_name!r}.")

    execution_id = uuid4().hex
    cancelled = threading.Event()
    previous_signals = _install_cancellation_signals(cancelled) if not arguments.dry_run else {}
    progress = _Progress(
        operation_id=arguments.operation_id,
        execution_id=execution_id,
        predecessor_execution_id=arguments.predecessor_execution_id,
        plan_digest=None,
        stage="start",
        completed=0,
        total=0,
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
            INGEST_OPERATOR_PROCESS,
            operation_id=arguments.operation_id,
            execution_id=execution_id,
            predecessor_execution_id=arguments.predecessor_execution_id or "none",
            plan_digest="unbound",
            stage="start",
            completed=0,
            total=0,
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
            _checkpoint(cancelled)
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
            s3 = session.client("s3")
            manifests = (
                (arguments.manifest,)
                if arguments.manifest is not None
                else _manifest_inputs(s3, arguments.bucket)
            )
            plan_digest = _plan_digest(manifests)
            total = len(manifests)
            progress = _Progress(
                operation_id=arguments.operation_id,
                execution_id=execution_id,
                predecessor_execution_id=arguments.predecessor_execution_id,
                plan_digest=plan_digest,
                stage="plan",
                completed=total if arguments.dry_run else 0,
                total=total,
            )
            root_span.record(
                plan_digest=plan_digest,
                stage="plan",
                completed=progress.completed,
                total=total,
            )
            _lifecycle(
                progress,
                phase="progress",
                outcome="completed",
                severity=LogSeverity.INFO,
            )
            if not manifests:
                print(f"  nothing landed in s3://{arguments.bucket}")  # noqa: T201
                _lifecycle(
                    progress,
                    phase="terminal",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                )
                return 0

            for manifest in manifests:
                print(f"  {manifest.key}  VersionId={manifest.version_id}")  # noqa: T201
            print(f"  total: {total} delivery marker(s)")  # noqa: T201
            print()  # noqa: T201

            if arguments.dry_run:
                _lifecycle(
                    progress,
                    phase="terminal",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                )
                print("Dry run. Nothing was written.")  # noqa: T201
                return 0

            def report_heartbeat(snapshot: _Progress) -> None:
                _lifecycle(
                    snapshot,
                    phase="heartbeat",
                    outcome="unchanged",
                    severity=LogSeverity.DEBUG,
                )

            # S6 (V-19): one strong mode read decides before any ingestion.
            gate = _operator_gate(session, arguments, str(identity.get("Arn", "")))
            decision = decide_gate(gate, manifests)
            for receipt in decision.receipts:
                print(  # noqa: T201
                    f"  {decision.mode.value}: {receipt.decision} "
                    f"{receipt.reason or ''} request={receipt.request_id}"
                )
            if not decision.ingest:
                outcome = "refused" if decision.path is GatePath.REFUSED else "admitted"
                _lifecycle(progress, phase="terminal", outcome=outcome, severity=LogSeverity.INFO)
                return 3 if decision.path is GatePath.REFUSED else 0

            heartbeat = _Heartbeat(report_heartbeat, progress)

            def report_progress(snapshot: _Progress) -> None:
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
                columns = load_registry(ROOT)
                contracts = tuple(contract for _, contract in load_contracts(ROOT))
                installed_packs = load_query_packs(ROOT)
                installed_governance = installed_governance_for(installed_packs)
                fetched: list[tuple[bool, str, LoadedDelivery]] = []
                failures = 0
                governed = GovernedMaterializationPort(arguments.bucket, s3)
                for manifest in manifests:
                    _checkpoint(cancelled)
                    try:
                        # S6 (hard deny (d)): the governed port refuses a historical
                        # manifest before any data-object read.
                        delivery = governed.fetch(manifest)
                    except LandingError as error:
                        failures += 1
                        print(  # noqa: T201
                            f"  REJECTED  {manifest.key}\n            {error}"
                        )
                        report_error(
                            ValidationError(
                                DELIVERY_REFUSED,
                                "Selected delivery failed immutable landing validation.",
                            ),
                            Report(
                                operation=_OPERATION,
                                correlation_id=execution_id,
                                logger=LOGGER,
                            ),
                        )
                        report_progress(
                            _Progress(
                                operation_id=arguments.operation_id,
                                execution_id=execution_id,
                                predecessor_execution_id=(arguments.predecessor_execution_id),
                                plan_digest=plan_digest,
                                stage="apply",
                                completed=failures,
                                total=total,
                            )
                        )
                        continue
                    fetched.append(
                        (
                            _writes_a_dimension(
                                delivery,
                                contracts,
                                installed_governance,
                            ),
                            manifest.key,
                            delivery,
                        )
                    )

                fetched.sort(key=lambda entry: (not entry[0], entry[1]))
                print(  # noqa: T201
                    f"Apply identity: account={identity.get('Account', '')} "
                    f"arn={identity.get('Arn', '')}"
                )
                semantic_resources = resolve_named_semantic_resources(
                    env_name,
                    cloudformation,
                    ssm,
                )
                _ingest_v2(
                    _V2Batch(
                        resolved=_semantic_names(semantic_resources),
                        s3=s3,
                        dynamodb=session.client("dynamodb"),
                        athena=session.client("athena"),
                        fetched=tuple(fetched),
                        columns=columns,
                        contracts=contracts,
                        installed_governance=installed_governance,
                        installed_discard_policies=discard_policies(installed_packs),
                        operation_id=arguments.operation_id,
                        execution_id=execution_id,
                        predecessor_execution_id=arguments.predecessor_execution_id,
                        plan_digest=plan_digest,
                        completed_before_load=failures,
                        total=total,
                        progress=report_progress,
                        cancelled=cancelled.is_set,
                    )
                )
            finally:
                heartbeat.stop()
            if decision.path is GatePath.LEGACY_THEN_SHADOW:
                record_shadow(gate, manifests, _report_shadow_failure(execution_id))
            progress = _Progress(
                operation_id=arguments.operation_id,
                execution_id=execution_id,
                predecessor_execution_id=arguments.predecessor_execution_id,
                plan_digest=plan_digest,
                stage="apply",
                completed=total,
                total=total,
            )
            outcome = "failed" if failures else "completed"
            root_span.record(stage="apply", completed=total, total=total, outcome=outcome)
            _lifecycle(
                progress,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.WARNING if failures else LogSeverity.INFO,
            )
            print()  # noqa: T201
            print(  # noqa: T201
                f"{total} V2 delivery marker(s), {failures} rejected before loading."
            )
            return 1 if failures else 0
    except IngestCancelledError, KeyboardInterrupt:
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


if __name__ == "__main__":
    sys.exit(main())
