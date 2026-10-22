# SPDX-License-Identifier: MIT-0
"""The V3 GRAPH bake (plan 4.6 under Addendum A3; design DB20-DB23, DB7, DB15).

Three units share one Baker invocation's composition (`BakeRuntime`):

- **`GraphBakeCoordinator`**, the `bake` node's work: the fan-out control. It makes the winner's
  only current selection (S5): each roster row's source renders once under the recording reader
  and the capturing query port, so the pass reads every CURRENT publication the row will use and
  binds its complete requests with zero Athena executions. It pins the manifest with the N
  distinct plan counts and their digest **before** `two_lane_partition`, then arms one
  `bake_task:<page>#<variant>` per roster row on its lane (`preset-bake-lane-0|1`), the PENDING
  `bake_finalize`, and the `ARM#` that arms it, in one transaction. The node settles without a
  contribution: the cohort's bake member is the finalizer's.
- **`GraphBakeTaskWork`**, one roster task per envelope: replay the pinned reads (`select_exact`,
  never CURRENT), bind again and refuse a request set or count that is not the pinned one
  (`bake_plan_count_mismatch`, zero queries), render through the invocation-shared admission,
  stage the objects (no pointer capability) and settle one closed `DurableBakeEffectV3`. A source
  refusal (`DomainError`, `ValidationError`) is the page's designed absence (`UNAVAILABLE`, its
  generated reason), whether the fan-out pinned the row refused or the render refused. A fatal
  failure (an invariant, the D18 body ceiling) or an exhausted retry settles the task FAILED with
  its `BAKE_RECOVERY#` obligation; a dependency failure with receives left retries.
- **The finalizer** (`bake_finalize`): armed once every task is terminal. A FAILED task, or its
  own deterministic error on its final receive, settles it FAILED with the cohort withheld
  (`_abandon_finalize`, ADR-140). Once every task succeeded and the precompute contribution
  is stored, it records the bake contribution from the joined effects and commits the winner's
  one final transaction: every effect and the contribution conditioned, page pointers for written
  effects only and only when the binding authorizes serving (ADR-139: `CURRENT` is the last-good
  generation by construction; a withheld cohort is signalled `GraphServingWithheld`; a stale
  precompute contribution settles the cohort withheld, ADR-140), the
  immutable FINALIZED receipt, the direct binding, the head and frontier drain
  and the run's completion. Under A3 it writes no semantic `GENERATION#` member: each run's
  publish flips and folds its own generation, losers included. Its first delivery arms the
  precompute readiness and stays unacknowledged with its redelivery shortened to
  `FINALIZE_ARMED_REDELIVERY_SECONDS` (the `ChangeMessageVisibility` override `graph_materialize`
  uses), so finalization waits for the precompute rather than a full 960 s visibility cycle.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from adapters.inbound.ingestion_handlers.graph_claim import (
    GraphClaimInput,
    claim_graph_envelope,
    release_claim_lease,
)
from adapters.jobs.execution_graph.node_verifier import UnreachablePostGrantFacts
from adapters.jobs.execution_graph.post_grant_consumer import (
    ConsumerContext,
    ConsumeResult,
    PostGrantEnvelope,
    PostGrantRuntime,
    PublishEffect,
    SettleWithoutContribution,
    WorkOutcome,
)
from adapters.jobs.execution_graph.release import (
    ReleaseStore,
    bind_once,
    record_contribution_once,
    serving_binding_for,
)
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.page_bake import (
    PROCESS_LEDGER,
    PageFatalError,
    PageParts,
    PageWindow,
    ProcessBakeLedger,
    ProcessEntry,
    RefusalLineage,
    classify_page_error,
    emit_isolated_refusals,
    guarded,
    page_absence,
    render_page,
    report_over_budget,
    stage_page,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeDelivery,
    BakeIntent,
    BakeV3Body,
    bake_finalize_body,
    bake_task_body,
)
from adapters.outbound.bake_sources import (
    BakeQueryCapture,
    BakeRowHorizon,
    BakeSourceReads,
    LimitResult,
    bake_request_digest,
    decode_reads,
    encode_reads,
    task_source,
)
from adapters.outbound.dynamodb.preset_artifacts import (
    pointer_candidate_item,
    pointer_from_candidate_item,
    pointer_transaction_items,
)
from adapters.outbound.telemetry.graph_metrics import (
    BAKE_TELEMETRY_LOSS_METRIC,
    BAKE_TRACE_INCOMPLETE_METRIC,
    BakeTaskReading,
    emit_bake_task,
    emit_bake_trace_signal,
    emit_isolated_refusal,
    emit_post_grant_invariant,
    emit_serving_withheld,
    emit_task_over_budget,
    process_memory_peak_mb,
)
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.span_context import (
    QueryStatistics,
    collecting,
    correlated,
    correlation,
)
from adapters.outbound.telemetry.tracing import (
    BAKE_FAN_OUT,
    BAKE_FINALIZE,
    BAKE_PHASE,
    BAKE_RUN,
    BAKE_SOURCE_PIN,
    BAKE_TASK,
    span,
    trace_boundary,
)
from domain.operations.bake_refusals import (
    STORED_REFUSAL_INVALID,
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalGroup,
    RefusalScope,
    classify_refusal,
    group_refusals,
)
from domain.operations.bake_scheduling import (
    PRECOMPUTE_READY_SECONDS,
    BakeTaskLoad,
    task_seconds,
    two_lane_partition,
)
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_STALE,
    TERMINAL_NODE_STATES,
    GraphGuardError,
    NodePhase,
    NodeState,
)
from domain.operations.execution_graph_release import (
    BAKE_FINALIZE_NODE,
    BAKE_PLAN_COUNT_MISMATCH,
    BAKE_TASK_NODE,
    DETERMINISTIC_INSIGHT_PAGES,
    DURABLE_BAKE_SOURCE_PIN_ABSENT,
    POST_GRANT_INVARIANT,
    BakeEffectKind,
    BakeRecoveryReason,
    BakeRoster,
    BakeRosterRow,
    BakeTaskSource,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    DurableBakeEffectInvalidError,
    DurableBakeEffectV3,
    DurableBakeSourceManifest,
    DurableBakeSourceManifestInvalidError,
    PinRefusal,
    PrecomputeInputV1,
    ServingBinding,
    bake_v3_digest,
    binding_authorizes_serving,
    failure_entries,
    join_bake_effects,
    serving_generation,
    serving_withheld,
    unavailable_reason,
    withheld_bake_contribution,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.operations.queue_clocks import queue_attributes
from domain.operations.run_request import RunState
from domain.presentation.preset_artifacts import (
    PresetArtifactBodyCeilingError,
    PresetArtifactPage,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPublicationRequest,
    PresetArtifactVariant,
    pointer_written_at,
)
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    NodeRecord,
    NodeSettlement,
    NodeSpec,
    RunRecord,
)
from ports.outbound.execution_graph_release import (
    BakeFanOutRequest,
    BakeFinalizeOutcome,
    BakeFinalizeRequest,
    BakeSourcePinRequest,
    BakeTaskFailure,
    PinnedBakeSources,
    PrecomputeReadinessRequest,
    ServingDomainHead,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

    from domain.presentation.preset_artifacts import PresetArtifactStageResult
    from ports.outbound.preset_artifacts import (
        PresetArtifactPointerStorePort,
        PresetArtifactStagePort,
    )

__all__ = [
    "BAKE_CLAIM_LEASE",
    "BAKE_INTENT_INVARIANT",
    "BAKE_TIMEOUT_SECONDS",
    "FINALIZE_ARMED_REDELIVERY_SECONDS",
    "PRECOMPUTE_ARMED",
    "BakeRuntime",
    "GraphBakeCoordinator",
    "GraphBakeRuntime",
    "GraphBakeTaskWork",
    "bake_contribution",
    "consume_bake_finalize",
    "consume_bake_task",
    "granted_request",
    "read_task_effect",
    "task_effect_record",
    "task_node_name",
]

BAKE_TIMEOUT_SECONDS: Final = int(BAKE_TASKS["lambda_timeout_seconds"])
"""The baker's Lambda timeout; `publisher-stack.ts` carries the same value."""
BAKE_CLAIM_LEASE: Final = timedelta(seconds=int(BAKE_TASKS["visibility_seconds"]))
"""Every bake claim's lease: the timeout plus the queue's visibility margin (960 s)."""
_OPERATION: Final = "graph_bake"
BAKE_INTENT_INVARIANT: Final = "bake_intent_invariant"
"""The POST_GRANT invariant signal of one roster task's fatal invariant (§2.11 row 5b)."""
_POINTER_CANDIDATE: Final = "graph-bake-pointer-candidate/v1"
_OUTPUT_MANIFEST: Final = "graph-bake-output-manifest/v1"
_FINALIZED: Final = "graph-bake-finalized/v1"
_FAN_OUT_EFFECT: Final = "graph-bake-fan-out/v1"
BAKE_FAN_OUT_FAILED: Final = ErrorCode("GRAPH_BAKE_V3", 10)
_MS_PER_TENTH: Final = 100
_FINAL_AUTHORITY_MEMBERS: Final = 10
"""The rolling final transaction's immutable-cohort authority members beside its N conditions
and pointer members (`final_transaction_member_count`)."""
TASK_PHASES: Final = ("plan", "query", "render", "write")
"""A2.1 task phases emitted as non-overlapping boundaries: pinned bind/count validation,
complete-bound query execution, result-only rendering, and artifact staging. The finalizer's wait is
recorded separately as `bake.finalize.wait_ms`."""
BAKE_TASK_FATAL: Final = ErrorCode("GRAPH_BAKE_V3", 11)
PRECOMPUTE_ARMED: Final = "graph_bake_precompute_armed"
"""The finalizer's first-delivery outcome: readiness armed, the message left unacknowledged."""
FINALIZE_ARMED_REDELIVERY_SECONDS: Final = PRECOMPUTE_READY_SECONDS
"""The armed nack's redelivery, instead of the queue's 960 s (owner: this module).

The armed precompute needs one graph dispatcher pass to be sent (its one-minute schedule plus a
pass of at most 60 s) and one precompute claim lease (30 s timeout + 30 s margin): 180 s, the
contract-derived `PRECOMPUTE_READY_SECONDS`. A
precompute that has not contributed by then leaves the redelivery `graph_bake_finalize_blocked`,
whose nack keeps the full queue visibility, so the final receive still follows a whole cycle.
Only the wait shortens; every receive still counts against `routes.bake.max_receive_count`."""


class RegisteredBaker(Protocol):
    """`RegisteredPresetArtifactBaker` as the V3 bake uses it."""

    def registered_pages(self) -> tuple[str, ...]:
        """Return the roster pages the registry renders."""
        ...

    def prefetch(self, intent: BakeIntent) -> None:
        """Read every CURRENT key one roster intent's real render can reach (A5.1, RECORD)."""
        ...

    def bake(self, intent: BakeIntent) -> BakeResult:
        """Render one roster intent."""
        ...


def task_node_name(key: str) -> str:
    """Return a roster task's node: `bake_task:<page>#<variant>` in `RUN#<run_id>`."""
    return f"{BAKE_TASK_NODE}:{key}"


def granted_request(
    request: PresetArtifactPublicationRequest, grant_id: str, written_at: str
) -> PresetArtifactPublicationRequest:
    """Bind a source's publication to this grant: `grant_id`, its generation and `written_at`."""
    advance = request.pointer_advance
    pointer = replace(
        advance.pointer,
        grant_id=grant_id,
        serving_generation=serving_generation(grant_id),
        written_at=written_at,
    )
    return replace(request, pointer_advance=replace(advance, pointer=pointer))


def _owner(run: RunRecord, cohort_revision: str) -> dict[str, str]:
    return {
        "grant_id": cast("str", run.grant_id),
        "granted_run": run.run_id,
        "conflict_unit": run.conflict_unit,
        "served_revision": datetime.fromisoformat(run.retrieved_at).isoformat(),
        "root_flow_id": run.root_flow_id,
        "cohort_revision": cohort_revision,
    }


class AdmissionObservation(Protocol):
    """The FIFO query admission's per-invocation observation (`AthenaBatchAdmission`)."""

    @property
    def maximum_occupancy(self) -> int:
        """The greatest simultaneous query occupancy since the last reset."""
        ...

    @property
    def waited_ms(self) -> int:
        """The total admission wait since the last reset."""
        ...

    def reset_observation(self) -> None:
        """Begin one task's observation."""
        ...


@dataclass
class BakeRuntime:
    """One invocation's V3 composition: the roster, the registry and the source authority."""

    roster: BakeRoster
    baker: RegisteredBaker
    reads: BakeSourceReads
    queries: BakeQueryCapture
    stage: PresetArtifactStagePort
    pointers: PresetArtifactPointerStorePort
    pointer_table: str
    query_cap: int
    """Q: the Baker's in-process start cap, the scheduler's query-wave width (plan 3.1)."""
    now: Callable[[], datetime]
    logger: Logger | None = None
    admission: AdmissionObservation | None = None
    """The invocation-shared FIFO admission, read for per-task occupancy and wait (plan 4.7)."""
    metrics: Callable[[BakeTaskReading], None] = emit_bake_task
    memory_peak_mb: Callable[[], float] = process_memory_peak_mb
    process: ProcessBakeLedger = PROCESS_LEDGER
    """The process's render ledger (`page_bake`); composition passes `PROCESS_LEDGER`."""
    live_payloads: int = 0
    peak_live_payloads: int = 0
    """One live variant payload at a time (plan 4.6); observed by tests."""
    telemetry_failures: int = 0
    """A2.2: telemetry losses counted in-process and logged; never a business failure."""
    trace_signal: Callable[[str, int], None] = emit_bake_trace_signal
    """A2: the alarmed telemetry-loss and trace-incompleteness counts."""
    serving_signal: Callable[[Literal[0, 1]], None] = emit_serving_withheld
    """ADR-139: the alarmed `GraphServingWithheld` gauge, emitted only by `_signal_serving`."""
    refusal_metric: Callable[[str, int], None] = emit_isolated_refusal
    """ADR-142: one `BakeIsolatedRefusal*` datapoint per refusal group."""
    over_budget_metric: Callable[[], None] = emit_task_over_budget
    """ADR-142: one `BakeTaskOverBudget` datapoint per over-budget settlement."""
    sleep: Callable[[float], None] = time.sleep
    """The pin pass's backoff sleep between dependency attempts (ADR-142)."""
    jitter: random.Random = field(default_factory=random.Random)
    """The pin pass's full-jitter source (not a secret)."""

    def log(
        self, phase: str, outcome: str, severity: LogSeverity = LogSeverity.INFO, **details: object
    ) -> None:
        """Report one lifecycle line; telemetry never changes the business result."""
        if self.logger is None:
            return
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component="graph_bake",
                phase=phase,
                outcome=outcome,
                severity=severity,
                details=details,
            ),
            self.logger,
        )

    def telemetry(self, emit: Callable[[], None]) -> None:
        """Run one telemetry emission; a failure is counted and logged WARNING, never raised."""
        guarded(emit, self._telemetry_lost)

    def _telemetry_lost(self, cause: str) -> None:
        """Count one telemetry loss, log it and signal it (plan 4.7, A2.2)."""
        self.telemetry_failures += 1
        # The log line and the loss count (A2) are independent: one failing never skips the other.
        _best_effort(
            lambda: self.log(
                "telemetry",
                "telemetry_unavailable",
                LogSeverity.WARNING,
                cause=cause,
                failures=self.telemetry_failures,
            )
        )
        _best_effort(lambda: self.trace_signal(BAKE_TELEMETRY_LOSS_METRIC, 1))


def _best_effort(report: Callable[[], None]) -> None:
    """Run one report of a telemetry loss; its own failure cannot be reported."""
    try:
        report()
    # error-propagation: guards the telemetry seam itself; a failed report cannot be reported
    except Exception:
        return


def _live_head(store: ReleaseStore, run: RunRecord) -> ServingDomainHead | None:
    """Read the winner's live domain head, or None when this run is not the ACTIVE winner."""
    if run.grant_id is None:
        return None
    execution = store.read_serving_execution(run.grant_id, run.run_id)
    if execution is None:
        return None
    head = store.read_serving_domain(execution.head.domain).head
    if head is None or head.state != "ACTIVE" or head.winner_run_id != run.run_id:
        return None
    return head


@dataclass(frozen=True, slots=True)
class _Armed:
    """What one fan-out did, for its `bake.run` span: adopted, or pinned and fanned out."""

    outcome: str
    winner_id: str | None
    manifest_digest: str | None
    lanes: int | None


@dataclass(frozen=True, slots=True)
class GraphBakeCoordinator:
    """The `bake` node's work: one selection per winner, pin, partition, arm (S5, D11)."""

    runtime: BakeRuntime

    @property
    def member(self) -> CohortMember:
        """The fan-out claims the cohort's `bake` node."""
        return CohortMember.BAKE

    @property
    def lease(self) -> timedelta:
        """The bake claim lease (960 s)."""
        return BAKE_CLAIM_LEASE

    def eligible(self, effect: PublishEffect) -> bool:
        """Bake consumes canonical curation only (§2.8 step 2)."""
        return effect.advanced_curation_count > 0

    def artifact_keys(self) -> tuple[str, ...]:
        """Every roster key, listed `post_grant_invariant` by a consumer invariant."""
        return self.runtime.roster.keys

    def counts(self) -> Mapping[str, int]:
        """Return the roster size, recorded on the settled node."""
        return {"tasks": len(self.runtime.roster.rows)}

    @trace_boundary(BAKE_RUN)
    def run(self, context: ConsumerContext) -> WorkOutcome:
        """Pin, partition and arm the roster inside the winner's `bake.run` span (A2.1)."""
        roster = self.runtime.roster
        run_id, task_id = context.run.run_id, context.node.node_id
        cohort = context.node.cohort_revision
        if cohort is None:
            raise ValueError("graph bake requires a cohort revision.")
        with (
            correlated(run_id, task_id, cohort),
            span(
                BAKE_RUN,
                run_id=run_id,
                task_id=task_id,
                cohort_revision=cohort,
                task_count=len(roster.keys),
                query_cap=self.runtime.query_cap,
            ) as recording,
        ):
            armed = self._fan_out(context)
            recording.record(
                winner_id=armed.winner_id,
                source_manifest_digest=armed.manifest_digest,
                lanes=armed.lanes,
                outcome=armed.outcome,
            )
            return self._fanned_out(roster)

    def _fan_out(self, context: ConsumerContext) -> _Armed:
        """Adopt an existing fan-out, or pin once and arm every task (replay-safe; S5)."""
        store = context.store
        roster = self.runtime.roster
        names = {node.name for node in store.read_nodes(context.run.run_id)}
        if {task_node_name(key) for key in roster.keys} <= names:
            return _Armed("adopted", None, None, None)
        pinned = store.read_durable_bake_sources(context.run.run_id)
        if pinned is None:
            pinned = self._select_and_pin(context)
        manifest = pinned.manifest.aligned_to(roster)
        head = _live_head(store, context.run)
        if head is None:
            raise InvariantError(
                BAKE_FAN_OUT_FAILED, "the bake fan-out's run is not the ACTIVE winner."
            )
        lanes = self._arm(context, manifest, head)
        return _Armed("fanned_out", head.winner_run_id, manifest.digest, lanes)

    @trace_boundary(BAKE_FAN_OUT)
    def _arm(
        self, context: ConsumerContext, manifest: DurableBakeSourceManifest, head: ServingDomainHead
    ) -> int:
        """Partition on the certified counts and arm every task in one transaction; return lanes.

        A result-dependent row's bind count undercounts its render, so each row's load is its
        certified upper bound (A5.3); load balancing only.
        """
        roster = self.runtime.roster
        groups = tuple(str(group) for group in BAKE_TASKS["lane_groups"])
        with span(
            BAKE_FAN_OUT,
            run_id=context.run.run_id,
            task_id=context.node.node_id,
            cohort_revision=cast("str", context.node.cohort_revision),
            tasks=len(roster.keys),
            lanes=len(groups),
        ) as recording:
            makespan_ms = self._partition_and_arm(context, (manifest, head))
            recording.record(makespan_ms=makespan_ms, outcome="armed")
            return len(groups)

    def _partition_and_arm(
        self,
        context: ConsumerContext,
        pinned: tuple[DurableBakeSourceManifest, ServingDomainHead],
    ) -> int:
        manifest, head = pinned
        store = context.store
        roster = self.runtime.roster
        groups = tuple(str(group) for group in BAKE_TASKS["lane_groups"])
        plan = two_lane_partition(
            tuple(
                BakeTaskLoad(
                    row.key,
                    task_seconds(
                        row.certified_plan_count, self.runtime.query_cap, row.write_seconds
                    ),
                )
                for row in roster.rows
            )
        )
        owner = _owner(context.run, cast("str", context.node.cohort_revision))
        tasks = tuple(
            NodeSpec(
                name=task_node_name(task.key),
                after=("bake",),
                phase=NodePhase.POST_GRANT,
                lock_key=f"POST_GRANT#{task_node_name(task.key)}#{context.run.conflict_unit}",
                group_id=groups[lane],
                body=bake_task_body(
                    owner,
                    page=task.page,
                    variant=task.variant,
                    lane=groups[lane],
                    manifest_digest=manifest.digest,
                    plan_count=task.plan_count,
                ),
            )
            for task, lane in zip(manifest.tasks, plan.lanes, strict=True)
        )
        finalize = NodeSpec(
            name=BAKE_FINALIZE_NODE,
            after=(BAKE_TASK_NODE,),
            phase=NodePhase.POST_GRANT,
            lock_key=f"POST_GRANT#{BAKE_FINALIZE_NODE}#{context.run.conflict_unit}",
            group_id=f"{BAKE_FINALIZE_NODE}#{context.run.conflict_unit}",
            body=bake_finalize_body(
                owner, manifest_digest=manifest.digest, roster_digest=roster.digest
            ),
        )
        authority = cast("Any", store).read_serving_publication_authority(context.run.run_id)
        store.fan_out_bake_tasks(
            BakeFanOutRequest(
                run=context.run,
                claim=context.claim,
                head=head,
                manifest_digest=manifest.digest,
                tasks=tasks,
                finalize=finalize,
                arm_attributes={
                    "grant_id": authority.grant_id,
                    "grant_ordinal": authority.grant_ordinal,
                    "domain": authority.domain,
                },
                recorded_at=context.now().isoformat(),
            )
        )
        self.runtime.log(
            "fan_out",
            "fanned_out",
            run_id=context.run.run_id,
            tasks=len(tasks),
            lanes=len(groups),
            makespan_tenths=max(plan.load_tenths),
        )
        return max(plan.load_tenths) * _MS_PER_TENTH

    def _fanned_out(self, roster: BakeRoster) -> SettleWithoutContribution:
        record = json.dumps(
            {"schema_version": "3", "node": "bake", "tasks": list(roster.keys)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return SettleWithoutContribution(
            record, bake_v3_digest(_FAN_OUT_EFFECT, record), len(roster.rows)
        )

    @trace_boundary(BAKE_SOURCE_PIN)
    def _select_and_pin(self, context: ConsumerContext) -> PinnedBakeSources:
        """Make the winner's one selection and pin inside its `bake.source_pin` span (A2.1)."""
        with span(
            BAKE_SOURCE_PIN,
            run_id=context.run.run_id,
            task_id=context.node.node_id,
            cohort_revision=cast("str", context.node.cohort_revision),
            task_count=len(self.runtime.roster.keys),
        ) as recording:
            pinned, attempts = self._selected_and_pinned(context)
            recording.record(pin_attempts=attempts, outcome="pinned")
            return pinned

    def _selected_and_pinned(self, context: ConsumerContext) -> tuple[PinnedBakeSources, int]:
        """Make the winner's one current selection: record, bind with no Athena, pin (DB21).

        A reclaim that finds the selection begun with no stored pin is fatal for this winner:
        no second current selection is ever made for it. Each row is isolated (F2): a taxonomy
        error other than a dependency pins that row with zero requests. A dependency failure is
        retried in place (`PIN_DEPENDENCY_ATTEMPTS`, ADR-142); a row whose attempts run out pins
        with zero requests and its `pin_refusal`, and the cohort proceeds. The pass's own
        release-store calls retry the same way and re-raise when theirs run out. Returns the pin
        and the most attempts any one row or call took.
        """
        store = context.store
        if context.claim.bake_selection_started:
            raise InvariantError(
                BAKE_FAN_OUT_FAILED,
                "the bake fan-out's selection began without a provable pin.",
                context={"signal": DURABLE_BAKE_SOURCE_PIN_ABSENT},
            )
        runtime = self.runtime
        _, attempts = _pin_retried(
            runtime, context, lambda: store.mark_bake_selection(context.claim)
        )
        sources: list[BakeTaskSource] = []
        blobs: list[bytes] = []
        # A5.1: one pinned instant and one snapshot for the whole selection; each row records
        # its reads and captures its bind requests in scopes that always restore on exit, and a
        # result-dependent row prefetches every CURRENT key its real render can reach first.
        with runtime.reads.clock.pinned(compute_instant(context.run)), runtime.reads.snapshot():
            for row in runtime.roster.rows:
                source, blob, row_attempts = self._pinned_row(context, row)
                sources.append(source)
                blobs.append(blob)
                attempts = max(attempts, row_attempts)
        manifest = DurableBakeSourceManifest(
            context.run.run_id,
            context.grant_id,
            runtime.roster.digest,
            tuple(sources),
        )
        head, head_attempts = _pin_retried(runtime, context, lambda: _live_head(store, context.run))
        if head is None:
            raise InvariantError(
                BAKE_FAN_OUT_FAILED, "the bake fan-out's run is not the ACTIVE winner."
            )
        live = head
        _, pin_attempts = _pin_retried(
            runtime,
            context,
            lambda: store.pin_durable_bake_sources(
                BakeSourcePinRequest(
                    context.run,
                    context.claim,
                    live,
                    manifest,
                    tuple(blobs),
                    context.now().isoformat(),
                )
            ),
        )
        runtime.log("source_pin", "pinned", run_id=context.run.run_id, tasks=len(sources))
        return PinnedBakeSources(manifest, tuple(blobs)), max(attempts, head_attempts, pin_attempts)

    def _pinned_row(
        self, context: ConsumerContext, row: BakeRosterRow
    ) -> tuple[BakeTaskSource, bytes, int]:
        """Record and bind one row, retrying a dependency failure; return its slice and attempts.

        Each attempt re-enters a fresh recording and capturing scope inside the one pinned
        instant and snapshot, so a failed attempt's partial reads are discarded.
        """
        runtime = self.runtime
        intent = BakeIntent(row.page, row.variant)
        attempt = 0
        while True:
            attempt += 1
            refusal: AdlyticsError | None = None
            try:
                with (
                    runtime.reads.recording(row_horizon(context.run, row)) as recorded,
                    runtime.queries.capturing_scope() as captured,
                ):
                    try:
                        _plan_pass(runtime, intent)
                    except DependencyError:
                        raise
                    # F2: one row's refusal or invariant never withholds its siblings. The row
                    # pins the reads seen up to it and zero requests; its task replays the same
                    # pass, so a refusal recurs as designed absence and an invariant settles FAILED.
                    except AdlyticsError as error:
                        refusal = error
            # error-propagation: retried, or logged row_refused and pinned as the row's pin_refusal
            except DependencyError as error:
                if _pin_backoff(runtime, context, attempt):
                    continue
                runtime.log(
                    "source_pin",
                    "row_refused",
                    LogSeverity.WARNING,
                    run_id=context.run.run_id,
                    key=row.key,
                    error_code=str(error.code),
                    reason=IsolatedRefusalReason.DEPENDENCY_UNAVAILABLE.value,
                    attempts=attempt,
                )
                refused = task_source(
                    row.page, row.variant, (), (), pin_refusal=PinRefusal(attempt)
                )
                return refused, encode_reads(()), attempt
            requests = () if refusal is not None else captured.requests
            if refusal is not None:
                runtime.log(
                    "source_pin",
                    "row_refused",
                    LogSeverity.WARNING,
                    run_id=context.run.run_id,
                    key=row.key,
                    **{"error_code": str(refusal.code), **_row_refusal(refusal)},
                )
            source = task_source(row.page, row.variant, recorded.reads, requests)
            return source, encode_reads(recorded.reads), attempt


PIN_DEPENDENCY_ATTEMPTS: Final = 3
"""The pin pass's attempts at one row or one release-store call on a `DependencyError`."""
PIN_WRITE_MARGIN_SECONDS: Final = 60
"""What a retry must leave of the invocation for the pin write itself."""
_PIN_BACKOFF_BASE_SECONDS: Final = 0.5
_PIN_BACKOFF_CAP_SECONDS: Final = 4.0


def _pin_backoff(runtime: BakeRuntime, context: ConsumerContext, failed: int) -> bool:
    """Sleep before the next attempt after `failed` failures, if attempts and time allow.

    Full jitter: `uniform(0, min(4, 0.5 * 2**failed))`; never when the sleep would leave less
    than `PIN_WRITE_MARGIN_SECONDS` of the invocation for the pin write.
    """
    if failed >= PIN_DEPENDENCY_ATTEMPTS:
        return False
    delay = runtime.jitter.uniform(
        0, min(_PIN_BACKOFF_CAP_SECONDS, _PIN_BACKOFF_BASE_SECONDS * 2**failed)
    )
    if context.remaining_ms() / 1_000 - delay < PIN_WRITE_MARGIN_SECONDS:
        return False
    runtime.sleep(delay)
    return True


def _pin_retried[T](
    runtime: BakeRuntime, context: ConsumerContext, call: Callable[[], T]
) -> tuple[T, int]:
    """Run one pin-pass release-store call, retrying a `DependencyError`; re-raise when out."""
    failed = 0
    while True:
        try:
            return call(), failed + 1
        except DependencyError:
            failed += 1
            if not _pin_backoff(runtime, context, failed):
                raise


def _row_refusal(refusal: AdlyticsError) -> dict[str, object]:
    """Return the pin's `row_refused` classified reason and allowlisted context (ADR-142).

    An invariant is no refusal: its line names only its code, and its task settles FAILED.
    """
    if not isinstance(refusal, DomainError | ValidationError):
        return {}
    classified = classify_refusal(refusal)
    return {"reason": classified.reason.value, **classified.refusal_context}


def _plan_pass(runtime: BakeRuntime, intent: BakeIntent) -> BakeResult:
    """Run one row's source sequence: the query-free prefetch, then the bind render (§1.1).

    The fan-out runs it recording, and each task's plan phase replays it, so a source refusal
    at either step recurs identically on every attempt (same pinned reads, clock and answers).
    """
    with runtime.queries.forbidden():
        runtime.baker.prefetch(intent)
    return runtime.baker.bake(intent)


def compute_instant(run: RunRecord) -> datetime:
    """Return the run's pinned instant, `compute_started_at` as aware UTC (A5.1 I6).

    `compute_started_at` is set once, by the run's first claim, so the fan-out and every task
    attempt read the same instant; no bake source reads a wall clock.
    """
    started = run.compute_started_at
    try:
        instant = None if started is None else datetime.fromisoformat(started)
    except ValueError:
        instant = None
    if instant is None or instant.tzinfo is None:
        raise InvariantError(BAKE_FAN_OUT_FAILED, "the bake run has no compute start instant.")
    return instant.astimezone(UTC)


def row_horizon(run: RunRecord, row: BakeRosterRow) -> BakeRowHorizon:
    """Return a row's declared source horizon, anchored on the run's compute start (I6, I9)."""
    return BakeRowHorizon(compute_instant(run).date(), row.source_horizon_days)


# --- tasks ------------------------------------------------------------------------------------


def _fatal_code(error: BaseException) -> str:
    if isinstance(error, PresetArtifactBodyCeilingError):
        return error.reason
    if isinstance(error, DurableBakeSourceManifestInvalidError | DurableBakeEffectInvalidError):
        return error.signal
    if isinstance(error, PageFatalError):
        return error.code
    if isinstance(error, AdlyticsError):
        return str(error.code)
    return type(error).__name__


_SOURCE_REFUSALS: Final = (DomainError, ValidationError)
"""F1 (ADR-139): a source's designed refusal. Caught only around the task's two baker calls
(its plan pass and its render), where it is the page's designed absence, never a failure."""


def _is_invariant(error: BaseException) -> bool:
    """Whether a task failure is an invariant (alarmed), not a refusal or the D18 ceiling."""
    return isinstance(
        error,
        InvariantError
        | DurableBakeSourceManifestInvalidError
        | DurableBakeEffectInvalidError
        | PageFatalError,
    )


def _is_fatal(error: BaseException) -> bool:
    """DB22/DB26: invariants, the D18 ceiling and other taxonomy errors are final.

    Dependencies retry. A source refusal never reaches it: the task maps it to designed absence
    at the baker call (F1).
    """
    if isinstance(error, DependencyError):
        return False
    return isinstance(error, AdlyticsError | PresetArtifactBodyCeilingError | PageFatalError)


def _output_manifest_digest(staged: PresetArtifactStageResult) -> str:
    return bake_v3_digest(
        _OUTPUT_MANIFEST,
        [
            staged.body.key,
            staged.body.digest,
            None if staged.custody is None else staged.custody.digest,
            [item.digest for item in staged.sibling_bodies],
            [item.digest for item in staged.sibling_custody],
        ],
    )


def _request_bytes(request: PresetArtifactPublicationRequest) -> int:
    """Return the encoded size of every object one stage call writes (body, custody, siblings)."""
    payloads = [
        request.body.payload_json,
        *(() if request.custody is None else (request.custody.payload_json,)),
        *(item.payload_json for item in request.sibling_bodies),
        *(item.payload_json for item in request.sibling_custody),
    ]
    return sum(len(payload.encode()) for payload in payloads)


def _candidate(advance: PresetArtifactPointerAdvanceRequest) -> dict[str, object]:
    expires = advance.superseded_history_expires_at
    return {
        "item": pointer_candidate_item(advance.pointer),
        "history_expires_at": None if expires is None else expires.isoformat(),
    }


def _advance_from(candidate: Mapping[str, object]) -> PresetArtifactPointerAdvanceRequest:
    expires = candidate.get("history_expires_at")
    return PresetArtifactPointerAdvanceRequest(
        pointer_from_candidate_item(cast("Any", candidate["item"])),
        None if expires is None else datetime.fromisoformat(str(expires)),
    )


@dataclass(frozen=True, slots=True)
class _TaskInputs:
    run: RunRecord
    node: NodeRecord
    head: ServingDomainHead
    intent: BakeIntent
    pinned: PinnedBakeSources
    observation: _TaskObservation
    remaining_ms: Callable[[], int]
    """The invocation's remaining time (the Lambda context's): the page's statement window."""
    final: bool
    """Whether this receive is the message's last (`BakeDelivery.final`)."""


@dataclass(frozen=True, slots=True)
class GraphBakeTaskWork:
    """One roster task: pinned replay, exact bind check, render, stage, one V3 effect."""

    runtime: BakeRuntime

    def effect_for(self, inputs: _TaskInputs) -> tuple[DurableBakeEffectV3, dict[str, object]]:
        """Return the task's closed effect and its stored record (no pointer capability).

        ADR-142, around the whole of the task's work (plan, render and stage): a pinned
        `pin_refusal` settles `dependency_unavailable` without replaying; a `DependencyError` is
        classified by `classify_page_error` (a bake-wait timeout on any receive, any other only on
        the final receive) into the page's designed absence, else it re-raises and retries.
        """
        runtime = self.runtime
        key = f"{inputs.intent.page}#{inputs.intent.variant}"
        source = inputs.pinned.manifest.task(key)
        row = runtime.roster.row(key)
        window = PageWindow(
            inputs.remaining_ms, row, float(BAKE_TASKS["tail_seconds"]), inputs.final
        )
        if source.pin_refusal is not None:
            refused = IsolatedRefusal(
                IsolatedRefusalReason.DEPENDENCY_UNAVAILABLE,
                RefusalScope.PAGE,
                attempts=source.pin_refusal.attempts,
            )
            return self._refused(inputs, source.digest, refused, window)
        try:
            return self._effect(inputs, (source, row), window)
        except DependencyError as error:
            refusal = classify_page_error(error, final=inputs.final)
            if refusal is None:
                raise
            inputs.observation.refusal_code = str(error.code)
            return self._refused(inputs, source.digest, refusal, window)

    def _effect(
        self,
        inputs: _TaskInputs,
        pinned: tuple[BakeTaskSource, BakeRosterRow],
        window: PageWindow,
    ) -> tuple[DurableBakeEffectV3, dict[str, object]]:
        """Replay the pinned plan pass, check its bind, render and stage (DB21, F1)."""
        runtime = self.runtime
        source, row = pinned
        index = inputs.pinned.manifest.tasks.index(source)
        reads = decode_reads(inputs.pinned.reads[index], source.reads_digest)
        observation = inputs.observation
        try:
            with (
                runtime.reads.clock.pinned(compute_instant(inputs.run)),
                runtime.reads.replaying(reads, row_horizon(inputs.run, row)),
            ):
                try:
                    with (
                        _phase(observation, "plan"),
                        runtime.queries.capturing_scope() as captured,
                    ):
                        _plan_pass(runtime, inputs.intent)
                except _SOURCE_REFUSALS as error:
                    # F1: the pinned replay refused as the fan-out's pass did (a refused pin
                    # carries zero requests): the row's designed absence, nothing executed.
                    refusal = classify_page_error(error, final=inputs.final)
                    if refusal is None:
                        raise
                    observation.refusal_code = str(error.code)
                    return self._refused(inputs, source.digest, refusal, window)
                bound = captured.requests
                digests = tuple(sorted(bake_request_digest(request) for request in bound))
                observation.query_count, observation.distinct_count = len(bound), len(set(digests))
                if digests != source.request_digests or len(set(digests)) != source.plan_count:
                    raise PageFatalError(BAKE_PLAN_COUNT_MISMATCH)
                result, limit = self._rendered(inputs, window)
                observation.executed_distinct = limit.distinct
                observation.executed_request_digests = limit.keys
                return self._classified(inputs, source.digest, result, window)
        finally:
            runtime.reads.done()

    def _rendered(self, inputs: _TaskInputs, window: PageWindow) -> tuple[BakeResult, LimitResult]:
        """Render through the one page execution, inside the pinned replay (ADR-142, ADR-144).

        `render_page` owns the statement deadline, the certified bound (over it is sticky and
        wins, A5.1 I5) and the classification: a source refusing on what its queries returned
        (a suppressed result) is the page's designed absence, with the executions it ran (F1),
        and its item and section refusals are the render's groups.
        """
        runtime = self.runtime
        observation = inputs.observation
        admission = runtime.admission
        if admission is not None:
            runtime.telemetry(admission.reset_observation)
        runtime.queries.observe(lambda name: _phase(observation, name))
        runtime.live_payloads += 1
        runtime.peak_live_payloads = max(runtime.peak_live_payloads, runtime.live_payloads)
        try:
            page = render_page(
                PageParts(runtime.baker, runtime.queries, runtime.process),
                inputs.intent,
                window,
            )
        finally:
            if admission is not None:
                observation.max_occupancy = admission.maximum_occupancy
                observation.admission_wait_ms = admission.waited_ms
            runtime.queries.observed()
            runtime.live_payloads -= 1
        observation.deadline_clamped = page.deadline_clamped
        observation.entry = page.entry
        observation.isolated = page.groups
        if page.cause is not None:
            observation.refusal_code = str(page.cause.code)
        return page.result, page.executed

    def _refused(  # noqa: PLR0913, PLR0917 -- the task, its source, its refusal and window
        self,
        inputs: _TaskInputs,
        source_digest: str,
        refusal: IsolatedRefusal,
        window: PageWindow,
    ) -> tuple[DurableBakeEffectV3, dict[str, object]]:
        """Settle the page's designed absence for one page refusal: its one group, no items."""
        inputs.observation.isolated = group_refusals((refusal,))
        if inputs.observation.refusal_code is None:
            inputs.observation.refusal_code = str(refusal.code)
        return self._classified(inputs, source_digest, page_absence(inputs.intent), window)

    def _classified(  # noqa: PLR0913, PLR0917 -- the task, its source, its result and window
        self, inputs: _TaskInputs, source_digest: str, result: BakeResult, window: PageWindow
    ) -> tuple[DurableBakeEffectV3, dict[str, object]]:
        run, intent = inputs.run, inputs.intent
        fields: dict[str, Any] = {
            "task_id": inputs.node.node_id,
            "run_id": run.run_id,
            "grant_id": cast("str", run.grant_id),
            "head_version": inputs.head.version,
            "page": intent.page,
            "variant": intent.variant,
            "source_manifest_digest": source_digest,
        }
        if isinstance(result, BakeUnchanged):
            effect = DurableBakeEffectV3(
                **fields,
                effect_kind=BakeEffectKind.UNCHANGED,
                reason_code="revision_current",
                artifact_revision=result.revision.value,
                output_manifest_digest=None,
                pointer_candidate_digest=None,
            )
            return effect, {"pointer": None}
        if isinstance(result, BakeUnavailable):
            if result.reason != unavailable_reason(intent.page):
                raise PageFatalError(result.reason)
            effect = DurableBakeEffectV3(
                **fields,
                effect_kind=BakeEffectKind.UNAVAILABLE,
                reason_code=result.reason,
                artifact_revision=None,
                output_manifest_digest=None,
                pointer_candidate_digest=None,
            )
            return effect, {"pointer": None}
        publication = result
        body = publication.request.body
        if (body.page.value, body.variant.value) != (intent.page, intent.variant):
            raise PageFatalError("bake_intent_mismatch")
        request = granted_request(
            publication.request, cast("str", run.grant_id), pointer_written_at(self.runtime.now())
        )
        with _phase(inputs.observation, "write"):
            staged = stage_page(self.runtime.stage.stage, request, window)
        if isinstance(staged, IsolatedRefusal):
            # ADR-145: the write deadline came first. No parent was written, so no custody
            # names the pairs already written; the page settles its designed absence.
            return self._refused(inputs, source_digest, staged, window)
        inputs.observation.pairs = 1 + len(request.sibling_bodies)
        inputs.observation.written_bytes = _request_bytes(request)
        candidate = _candidate(request.pointer_advance)
        effect = DurableBakeEffectV3(
            **fields,
            effect_kind=BakeEffectKind.WRITTEN,
            reason_code=None,
            artifact_revision=body.revision.value,
            output_manifest_digest=_output_manifest_digest(staged),
            pointer_candidate_digest=bake_v3_digest(_POINTER_CANDIDATE, candidate),
        )
        return effect, {"pointer": candidate}


def task_effect_record(effect: DurableBakeEffectV3, extra: Mapping[str, object]) -> str:
    """Render a task node's effect record: the closed effect and its pointer candidate."""
    return json.dumps(
        {
            "schema_version": "3",
            "node": BAKE_TASK_NODE,
            "effect": effect.to_record(),
            **extra,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def read_task_effect(node: NodeRecord) -> tuple[DurableBakeEffectV3, dict[str, object]]:
    """Parse a settled task's effect record and refuse one that does not re-hash (DB22)."""
    if node.effect_record is None or node.effect_digest is None:
        raise DurableBakeEffectInvalidError("a settled task has no effect record")
    try:
        parsed = cast("dict[str, object]", json.loads(node.effect_record))
        effect = DurableBakeEffectV3.from_record(cast("dict[str, object]", parsed["effect"]))
    except KeyError, TypeError, ValueError:
        raise DurableBakeEffectInvalidError("a task effect record is malformed") from None
    if effect.effect_digest != node.effect_digest or effect.task_id != node.node_id:
        raise DurableBakeEffectInvalidError("a task effect does not re-hash to its node digest")
    pointer = parsed.get("pointer")
    if (pointer is None) != (effect.effect_kind is not BakeEffectKind.WRITTEN):
        raise DurableBakeEffectInvalidError("a pointer candidate disagrees with its effect kind")
    if pointer is not None and (
        bake_v3_digest(_POINTER_CANDIDATE, pointer) != effect.pointer_candidate_digest
    ):
        raise DurableBakeEffectInvalidError("a pointer candidate does not re-hash")
    return effect, parsed


@dataclass(frozen=True, slots=True)
class BakePorts:
    """The store, clock, logger and invariant counter one V3 consume runs with."""

    store: ReleaseStore
    now: Callable[[], datetime]
    logger: Logger | None = None
    invariant_metric: Callable[[str], None] = emit_post_grant_invariant
    visibility: Callable[[str, str, int], None] | None = None
    """Set one message's visibility: (queue URL, receipt handle, seconds)."""


def _report(ports: BakePorts, delivery: BakeDelivery, error: BaseException) -> None:
    if ports.logger is None:
        return
    report_error(
        error,
        Report(operation=_OPERATION, correlation_id=delivery.body.node_id, logger=ports.logger),
    )


def _claim(ports: BakePorts, delivery: BakeDelivery) -> tuple[ClaimRecord | None, ConsumeResult]:
    enqueued, received = queue_attributes(
        {
            name: value
            for name, value in (
                ("SentTimestamp", delivery.sent_timestamp),
                ("ApproximateFirstReceiveTimestamp", delivery.first_receive_timestamp),
            )
            if value is not None
        }
    )
    disposition = claim_graph_envelope(
        ports.store,
        UnreachablePostGrantFacts(),
        GraphClaimInput(
            run_id=delivery.body.run_id,
            node_id=delivery.body.node_id,
            dispatch_id=delivery.body.dispatch_id,
            body=delivery.body_text,
            now=ports.now,
            lease=BAKE_CLAIM_LEASE,
            queue_enqueued_at=None if enqueued is None else enqueued.isoformat(),
            first_received_at=None if received is None else received.isoformat(),
        ),
    )
    return disposition.claim, ConsumeResult(disposition.acknowledge, disposition.outcome)


def _node(store: ReleaseStore, run_id: str, node_id: str) -> NodeRecord | None:
    return next((item for item in store.read_nodes(run_id) if item.node_id == node_id), None)


@trace_boundary(BAKE_TASK)
def consume_bake_task(  # noqa: PLR0913, PLR0917 -- the task's ports, message, work and window
    ports: BakePorts,
    delivery: BakeDelivery,
    work: GraphBakeTaskWork,
    remaining_ms: Callable[[], int],
) -> ConsumeResult:
    """Claim and run one roster task inside its `bake.task` span (plan 4.7, A2.1).

    Every nested span (its phases, each `athena.query`, each `artifact.write`) carries the run and
    task ids; the task ends with one summary line and its metrics, both non-fatal.
    """
    sent, runtime = delivery.body, work.runtime
    cohort = sent.owner["cohort_revision"]
    key = f"{sent.page}#{sent.variant}"
    budget = next((item.budget_seconds for item in runtime.roster.rows if item.key == key), None)
    observation = _TaskObservation()
    started = monotonic()
    with (
        correlated(sent.run_id, sent.node_id, cohort),
        collecting() as statistics,
        span(
            BAKE_TASK,
            run_id=sent.run_id,
            task_id=sent.node_id,
            cohort_revision=cohort,
            page=sent.page,
            variant=sent.variant,
            lane=sent.lane,
            attempt=delivery.receive_count,
            planned_query_count=sent.plan_count,
            budget_seconds=budget,
        ) as recording,
    ):
        result = _consume_task(ports, delivery, work, observation, remaining_ms)
        recording.record(
            effect_kind=observation.effect_kind,
            reason=observation.reason,
            query_count=observation.query_count,
            distinct_count=observation.distinct_count,
            max_occupancy=observation.max_occupancy,
            queue_ms=sum(item.queue_ms for item in statistics),
            admission_wait_ms=observation.admission_wait_ms,
            pairs=observation.pairs,
            bytes=observation.written_bytes,
            isolated_refusals=_group_names(_emitted(observation, result)[0]),
            over_budget=_emitted(observation, result)[1] is not None or None,
            outcome=result.outcome,
        )
        report = _TaskReport(
            delivery, observation, tuple(statistics), result, monotonic() - started
        )
        runtime.telemetry(lambda: _report_task(runtime, report))
        return result


@dataclass(slots=True)
class _TaskObservation:
    """What one task measured, for its span, its summary line and its metrics (4.7, A2)."""

    phase_ms: dict[str, int] = field(default_factory=dict[str, int])
    effect_kind: str | None = None
    reason: str | None = None
    query_count: int | None = None
    distinct_count: int | None = None
    max_occupancy: int | None = None
    admission_wait_ms: int | None = None
    pairs: int | None = None
    written_bytes: int | None = None
    executed_distinct: int | None = None
    """The render's distinct executed requests, at most the row's certified bound (A5.1 I5)."""
    executed_request_digests: tuple[str, ...] = ()
    """Their bytewise-sorted canonical `<kind>:<digest>` keys."""
    refusal_code: str | None = None
    """The source refusal a designed-absence effect was mapped from (F1), if any."""
    deadline_clamped: bool = False
    """Whether the invocation reported negative remaining time, read as zero (ADR-144)."""
    isolated: tuple[RefusalGroup, ...] = ()
    """This receive's `(scope, reason)` refusal groups (ADR-142), stored with its settlement."""
    over_budget: _OverBudget | None = None
    """The settlement's seconds over its row's budget, stored with it (ADR-142)."""
    settled: bool = False
    """Set at one line only: after `settle_node` returns for this receive's SUCCEEDED."""
    replayed: _Stored | None = None
    """A settled node's stored groups, replayed by a redelivery (`_replay_settled`)."""
    entry: ProcessEntry | None = None
    """The render's process-ledger entry (02 §3.6); None when the task didn't render."""
    started: float = field(default_factory=monotonic)
    """The task's start (monotonic), for its settled seconds against its budget."""


@dataclass(frozen=True, slots=True)
class _OverBudget:
    """A settlement's seconds and the budget they exceeded (stored, replayed)."""

    seconds: float
    budget_seconds: float

    def to_record(self) -> dict[str, float]:
        return {"seconds": self.seconds, "budget_seconds": self.budget_seconds}

    @classmethod
    def from_record(cls, record: object) -> _OverBudget | None:
        if record is None:
            return None
        if not isinstance(record, dict) or set(cast("dict[str, object]", record)) != {
            "seconds",
            "budget_seconds",
        }:
            raise ValueError("a stored over-budget report has exactly two fields.")
        fields = cast("dict[str, object]", record)
        values = (fields["seconds"], fields["budget_seconds"])
        if any(isinstance(item, bool) or not isinstance(item, int | float) for item in values):
            raise ValueError("a stored over-budget report's fields are numbers.")
        return cls(float(cast("float", values[0])), float(cast("float", values[1])))


@dataclass(frozen=True, slots=True)
class _Stored:
    """What a settled task stored for its emission: its groups and its over-budget report."""

    groups: tuple[RefusalGroup, ...]
    over_budget: _OverBudget | None


def _emitted(
    observation: _TaskObservation, result: ConsumeResult
) -> tuple[tuple[RefusalGroup, ...], _OverBudget | None]:
    """Return what this receive emits: its own settlement's, a replayed one's, or nothing.

    The predicate is this receive's own settlement (`observation.settled`), checked against the
    consumer's result so the two can't disagree silently; never the outcome string.
    """
    if observation.settled and result.node_state is NodeState.SUCCEEDED:
        return observation.isolated, observation.over_budget
    if observation.replayed is not None:
        return observation.replayed.groups, observation.replayed.over_budget
    return (), None


def _group_names(groups: tuple[RefusalGroup, ...]) -> str | None:
    return ",".join(f"{group.scope.value}:{group.reason.value}" for group in groups) or None


@trace_boundary(BAKE_PHASE)
@contextmanager
def _phase(observation: _TaskObservation, phase: str) -> Generator[None]:
    """Own one task phase's `bake.phase` span and add its duration to the task's totals."""
    corr = correlation()
    if corr is None:
        raise ValueError("graph bake phase requires cohort span correlation.")
    started = monotonic()
    with span(
        BAKE_PHASE,
        run_id=corr.run_id,
        task_id=corr.task_id,
        cohort_revision=corr.cohort_revision,
        phase=phase,
    ) as recording:
        try:
            yield
        finally:
            elapsed = int((monotonic() - started) * 1_000)
            observation.phase_ms[phase] = observation.phase_ms.get(phase, 0) + elapsed
        recording.record(outcome="completed")


def _percentile50(values: list[int]) -> int:
    """Return the nearest-rank median (the lower middle), 0 for no values."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2] if ordered else 0


def query_rollup(statistics: tuple[QueryStatistics, ...]) -> dict[str, int]:
    """A2.3: one task's per-query roll-up: count, and total/p50/max of queue, engine, total."""
    rolled: dict[str, int] = {
        "query_executions": len(statistics),
        "statistics_unavailable": sum(not item.statistics_available for item in statistics),
        "scanned_bytes": sum(item.scanned_bytes for item in statistics),
        "admission_wait_ms_total": sum(item.admission_wait_ms for item in statistics),
        "throttled_starts": sum(item.throttled_starts for item in statistics),
    }
    for name in ("queue_ms", "engine_ms", "total_ms"):
        values = [int(getattr(item, name)) for item in statistics]
        rolled[f"{name}_total"] = sum(values)
        rolled[f"{name}_p50"] = _percentile50(values)
        rolled[f"{name}_max"] = max(values, default=0)
    return rolled


@dataclass(frozen=True, slots=True)
class _TaskReport:
    """One finished task, as its metrics and summary line read it."""

    delivery: BakeDelivery
    observation: _TaskObservation
    statistics: tuple[QueryStatistics, ...]
    result: ConsumeResult
    seconds: float


def _report_task(runtime: BakeRuntime, report: _TaskReport) -> None:
    """Emit one task's metrics (settled tasks only) and its one structured summary line."""
    delivery, observation, statistics = report.delivery, report.observation, report.statistics
    result, seconds = report.result, report.seconds
    page, variant = cast("str", delivery.body.page), cast("str", delivery.body.variant)
    if observation.effect_kind is not None:
        runtime.metrics(
            BakeTaskReading(
                page,
                variant,
                observation.effect_kind,
                round(seconds, 3),
                runtime.memory_peak_mb(),
                observation.query_count or 0,
                observation.pairs or 0,
            )
        )
    details: dict[str, object] = {
        "run_id": delivery.body.run_id,
        "task_id": delivery.body.node_id,
        "page": page,
        "variant": variant,
        "effect_kind": observation.effect_kind or "none",
        "seconds": round(seconds, 3),
        **{f"phase_{name}_ms": observation.phase_ms.get(name, 0) for name in TASK_PHASES},
        **query_rollup(statistics),
        "max_occupancy": observation.max_occupancy or 0,
        "pairs": observation.pairs or 0,
        "written_bytes": observation.written_bytes or 0,
        "executed_distinct": observation.executed_distinct or 0,
        "executed_request_digests": list(observation.executed_request_digests),
        "deadline_clamped": observation.deadline_clamped,
        **_memory_fields(runtime, observation.entry),
    }
    runtime.log("task_summary", result.outcome, LogSeverity.INFO, **details)
    groups, over_budget = _emitted(observation, result)
    replayed = observation.replayed is not None and not observation.settled
    lineage = RefusalLineage(
        "graph", page, variant, run_id=delivery.body.run_id, task_id=delivery.body.node_id
    )
    if groups:
        runtime.telemetry(
            lambda: emit_isolated_refusals(
                runtime.logger, groups, lineage, replayed=replayed, metric=runtime.refusal_metric
            )
        )
    if over_budget is not None:
        row = runtime.roster.row(f"{page}#{variant}")
        runtime.telemetry(
            lambda: _report_over_budget(runtime, over_budget, row, lineage, replayed=replayed)
        )
    incomplete = trace_incomplete_count(statistics)
    if incomplete:
        runtime.trace_signal(BAKE_TRACE_INCOMPLETE_METRIC, incomplete)


def _report_over_budget(  # noqa: PLR0913, PLR0917 -- the runtime, stored report, row, lineage
    runtime: BakeRuntime,
    over_budget: _OverBudget,
    row: BakeRosterRow,
    lineage: RefusalLineage,
    *,
    replayed: bool,
) -> None:
    """Report a settlement's stored over-budget seconds once (the settling receive or replay)."""
    report_over_budget(
        runtime.logger,
        over_budget.seconds,
        replace(row, budget_seconds=over_budget.budget_seconds),
        lineage,
        replayed=replayed,
        metric=runtime.over_budget_metric,
    )


def _memory_fields(runtime: BakeRuntime, entry: ProcessEntry | None) -> dict[str, object]:
    """Return the page observation's memory and process fields (02 §3.6) for a summary.

    `memory_peak_mb` is the process's peak so far (`ru_maxrss`), read through the ledger; the
    entry fields are absent (None) for a task that didn't render.
    """
    return {
        "memory_peak_mb": runtime.process.peak(),
        "rss_before_mb": None if entry is None else entry.rss_before_mb,
        "process_baseline_mb": None if entry is None else entry.process_baseline_mb,
        "process_id": None if entry is None else entry.process_id,
        "process_bake_ordinal": None if entry is None else entry.process_bake_ordinal,
    }


def trace_incomplete_count(statistics: tuple[QueryStatistics, ...]) -> int:
    """A2.4: succeeded executions whose `athena.query` span has no statistics.

    A failed or cancelled execution is complete without statistics (its terminal state is the
    record, A2.2); only a succeeded one without them is a gap in the readout.
    """
    return sum(item.state == "SUCCEEDED" and not item.statistics_available for item in statistics)


def _consume_task(  # noqa: PLR0911, PLR0913, PLR0917 -- one ordered task lifecycle, observed
    ports: BakePorts,
    delivery: BakeDelivery,
    work: GraphBakeTaskWork,
    observation: _TaskObservation,
    remaining_ms: Callable[[], int],
) -> ConsumeResult:
    """Claim and run one roster task; settle its effect, or FAILED with its recovery owner.

    `remaining_ms` is the invocation's remaining time, the window `render_page` sets the page's
    statement deadline from.
    """
    claim, refused = _claim(ports, delivery)
    if claim is None:
        if refused.outcome == "graph_already_settled":
            _replay_settled(ports, delivery, observation, None)
        return refused
    store, body = ports.store, delivery.body
    run = store.read_run(body.run_id)
    node = _node(store, body.run_id, body.node_id)
    if run is None or node is None or run.state is not RunState.GRANTED:
        return ConsumeResult(True, "graph_stale_execution_attempt")
    if node.state in TERMINAL_NODE_STATES:
        _replay_settled(ports, delivery, observation, node)
        return ConsumeResult(True, "graph_already_settled", node.state)
    head = _live_head(store, run)
    if head is None or run.grant_id != body.owner["grant_id"]:
        return ConsumeResult(True, "graph_stale_execution_attempt")
    try:
        pinned = store.read_durable_bake_sources(run.run_id)
        if pinned is None or pinned.manifest.digest != body.manifest_digest:
            raise PageFatalError(DURABLE_BAKE_SOURCE_PIN_ABSENT)
        intent = BakeIntent(cast("str", body.page), cast("str", body.variant))
        effect, extra = work.effect_for(
            _TaskInputs(run, node, head, intent, pinned, observation, remaining_ms, delivery.final)
        )
    except Exception as error:
        _report(ports, delivery, error)
        if _is_invariant(error):
            ports.invariant_metric(BAKE_INTENT_INVARIANT)
        if _is_fatal(error) or delivery.final:
            reason = (
                BakeRecoveryReason.FATAL if _is_fatal(error) else BakeRecoveryReason.RETRY_EXHAUSTED
            )
            observation.effect_kind, observation.reason = "failed", _fatal_code(error)
            return _fail_task(ports, (run, node, claim), reason, _fatal_code(error))
        observation.reason = "retryable"
        release_claim_lease(store, claim, ports.now())
        return ConsumeResult(False, "graph_bake_task_retryable", NodeState.CLAIMED)
    observation.effect_kind = effect.effect_kind.value
    observation.reason = effect.reason_code or "none"
    row = work.runtime.roster.row(effect.key)
    seconds = round(monotonic() - observation.started, 3)
    observation.over_budget = (
        _OverBudget(seconds, row.budget_seconds) if seconds > row.budget_seconds else None
    )
    # ADR-142: the groups travel inside the settlement, so a lost settle response replays them.
    # `extra` isn't covered by the effect's digest, and `read_task_effect` reads only the effect
    # and the pointer, so they change no digest and no finalizer read.
    stored = {
        **extra,
        "isolated_refusals": [group.to_record() for group in observation.isolated],
        "over_budget": None
        if observation.over_budget is None
        else observation.over_budget.to_record(),
    }
    try:
        store.settle_node(
            NodeSettlement(
                run,
                node,
                NodeState.SUCCEEDED,
                ports.now().isoformat(),
                claim=claim,
                effect_digest=effect.effect_digest,
                effect_record=task_effect_record(effect, stored),
            )
        )
    except GraphGuardError as guard:
        if guard.signal != CLAIM_LEASE_STALE:
            raise
        return ConsumeResult(True, "graph_claim_lost")
    observation.settled = True  # this receive's own settlement, nothing else
    work.runtime.log(
        "task",
        effect.effect_kind.value,
        run_id=run.run_id,
        task_id=node.node_id,
        page=effect.page,
        variant=effect.variant,
        reason=effect.reason_code or "none",
        refusal_code=observation.refusal_code or "none",
    )
    return ConsumeResult(True, f"graph_bake_task_{effect.effect_kind.value}", NodeState.SUCCEEDED)


def _replay_settled(  # noqa: PLR0913, PLR0917 -- the ports, delivery, observation, node
    ports: BakePorts,
    delivery: BakeDelivery,
    observation: _TaskObservation,
    node: NodeRecord | None,
) -> None:
    """Load a settled task's stored groups for this redelivery to emit once (ADR-142, M1).

    Both `graph_already_settled` sites call it: the claim decision (no node read yet, so it reads
    one) and after the claim (the node in hand). A SUCCEEDED node's stored groups and over-budget
    report are rebuilt through the refusal's own validation; any other node emits nothing. A
    stored list that doesn't validate is a defect (`STORED_REFUSAL_INVALID`), reported and
    counted on the alarmed `bake_intent_invariant` signal, and the receive is still
    acknowledged. A `DependencyError` from the node read raises: the record retries and, on its
    last receive, dead-letters.
    """
    body = delivery.body
    found = node if node is not None else _node(ports.store, body.run_id, body.node_id)
    if found is None or found.state is not NodeState.SUCCEEDED or found.effect_record is None:
        return
    try:
        parsed = cast("dict[str, object]", json.loads(found.effect_record))
        listed = parsed.get("isolated_refusals")
        if listed is not None and not isinstance(listed, list):
            raise ValueError("stored isolated refusals are a list.")
        groups = tuple(
            RefusalGroup.from_record(item) for item in cast("list[object]", listed or [])
        )
        over_budget = _OverBudget.from_record(parsed.get("over_budget"))
    except (TypeError, ValueError) as error:
        invalid = InvariantError(
            STORED_REFUSAL_INVALID,
            "A settled bake task's stored refusal groups do not validate.",
            context={"signal": "STORED_REFUSAL_INVALID"},
        )
        invalid.__cause__ = error
        _report(ports, delivery, invalid)
        # The baker's alarmed per-intent invariant signal; the report names STORED_REFUSAL_INVALID.
        ports.invariant_metric(BAKE_INTENT_INVARIANT)
        return
    if groups or over_budget is not None:
        observation.replayed = _Stored(groups, over_budget)


def _fail_task(  # noqa: PLR0913, PLR0917 -- the held task, its reason and code
    ports: BakePorts,
    held: tuple[RunRecord, NodeRecord, ClaimRecord],
    reason: BakeRecoveryReason,
    code: str,
) -> ConsumeResult:
    run, node, claim = held
    try:
        ports.store.settle_bake_task_failure(
            BakeTaskFailure(run, node, claim, reason.value, code, ports.now().isoformat())
        )
    except GraphGuardError:
        return ConsumeResult(True, "graph_claim_lost")
    return ConsumeResult(True, f"graph_bake_task_{reason.value}", NodeState.FAILED, code)


# --- the finalizer --------------------------------------------------------------------------


def bake_contribution(
    run: RunRecord, effects: tuple[DurableBakeEffectV3, ...], at: datetime
) -> CohortContribution:
    """Derive the cohort's bake member from the joined effects (D26/D27 semantics kept)."""
    grant = cast("str", run.grant_id)
    unavailable = tuple(
        sorted(
            (effect.key, cast("str", effect.reason_code))
            for effect in effects
            if effect.effect_kind is BakeEffectKind.UNAVAILABLE
        )
    )
    produced = any(effect.effect_kind is not BakeEffectKind.UNAVAILABLE for effect in effects)
    if unavailable:
        outcome = CohortOutcome.UNAVAILABLE
    elif produced:
        outcome = CohortOutcome.SUCCEEDED
    else:
        outcome = CohortOutcome.NO_ELIGIBLE
    return CohortContribution(
        grant,
        CohortMember.BAKE,
        outcome,
        serving_generation(grant) if produced else None,
        at,
        run.root_flow_id,
        unavailable,
    )


def _precompute_input(
    runtime: BakeRuntime,
    parsed: tuple[tuple[DurableBakeEffectV3, dict[str, object]], ...],
) -> PrecomputeInputV1:
    """Bind every available effect to its exact staged or authorized immutable manifest."""
    manifests: dict[str, tuple[str, str, str | None]] = {}
    for effect, extra in parsed:
        if effect.effect_kind is BakeEffectKind.UNAVAILABLE:
            continue
        pointer = (
            _advance_from(cast("Mapping[str, object]", extra["pointer"])).pointer
            if effect.effect_kind is BakeEffectKind.WRITTEN
            else runtime.pointers.current(
                PresetArtifactPage(effect.page), PresetArtifactVariant(effect.variant)
            )
        )
        if (
            pointer is None
            or pointer.revision.value != effect.artifact_revision
            or (pointer.custody_digest is None and effect.page not in DETERMINISTIC_INSIGHT_PAGES)
        ):
            raise InvariantError(
                BAKE_FAN_OUT_FAILED,
                "an available bake effect has no exact authorized body/custody manifest.",
            )
        manifests[effect.key] = (
            pointer.body_key,
            pointer.body_digest,
            pointer.custody_digest,
        )
    return PrecomputeInputV1.from_effects_and_manifests(
        tuple(effect for effect, _ in parsed), manifests
    )


@dataclass(frozen=True, slots=True)
class _FinalizeInputs:
    run: RunRecord
    node: NodeRecord
    claim: ClaimRecord
    head: ServingDomainHead


@dataclass(frozen=True, slots=True)
class _Abandon:
    """The finalizer settles the cohort withheld instead of committing (ADR-140, B4/F5)."""

    cause: str
    """`bake_task_failed` (a task settled FAILED) or `finalize_error` (its own final receive)."""
    code: str
    """The finalizer node's `failure_code`."""


_TASK_FAILED: Final = "bake_task_failed"


def _finalize_request(
    ports: BakePorts, inputs: _FinalizeInputs, runtime: BakeRuntime
) -> BakeFinalizeRequest | ConsumeResult | _Abandon:
    store, run = ports.store, inputs.run
    by_name = {node.name: node for node in store.read_nodes(run.run_id)}
    tasks = tuple(by_name.get(task_node_name(key)) for key in runtime.roster.keys)
    if any(task is None or task.state not in TERMINAL_NODE_STATES for task in tasks):
        return ConsumeResult(False, "graph_bake_finalize_blocked", NodeState.CLAIMED)
    if any(cast("NodeRecord", task).state is not NodeState.SUCCEEDED for task in tasks):
        # B4: a terminal task never changes, so this is decided on the first receive.
        return _Abandon(_TASK_FAILED, _TASK_FAILED)
    settled = tuple(cast("NodeRecord", task) for task in tasks)
    parsed = tuple(read_task_effect(task) for task in settled)
    effects = join_bake_effects(runtime.roster, (effect for effect, _ in parsed))
    readiness = _precompute_input(runtime, parsed)
    precompute_node = by_name.get("precompute")
    if precompute_node is None:
        raise InvariantError(BAKE_FAN_OUT_FAILED, "a winner has no precompute node.")
    if precompute_node.state is NodeState.WAITING_INPUT:
        store.arm_precompute_from_bake_effects(
            PrecomputeReadinessRequest(
                run,
                inputs.claim,
                inputs.head,
                precompute_node,
                runtime.roster,
                settled,
                effects,
                readiness,
                ports.now().isoformat(),
            )
        )
        return ConsumeResult(False, PRECOMPUTE_ARMED, NodeState.CLAIMED)
    stored_input = store.read_precompute_input(run.run_id)
    if stored_input != readiness:
        raise InvariantError(
            BAKE_FAN_OUT_FAILED,
            "the precompute node is not bound to the exact bake-effect readiness input.",
        )
    grant = cast("str", run.grant_id)
    precompute = next(
        (
            item
            for item in store.read_contributions(grant)
            if item.member is CohortMember.PRECOMPUTE
        ),
        None,
    )
    if precompute is None:
        return ConsumeResult(False, "graph_bake_finalize_blocked", NodeState.CLAIMED)
    if (
        precompute.input_effect_roster_digest != readiness.digest
        or tuple(key for key, _ in precompute.request_roster) != runtime.roster.keys
    ):
        # ADR-140 (Z3): a stale precompute (an older precompute's NO_ELIGIBLE with no digest,
        # or an invariant's) settles the cohort withheld; every page keeps its last-good pointer.
        runtime.log(
            "finalize",
            "stale_precompute",
            LogSeverity.WARNING,
            run_id=run.run_id,
            precompute_outcome=precompute.outcome.value,
            has_digest=precompute.input_effect_roster_digest is not None,
        )
        contribution = withheld_bake_contribution(
            grant, run.root_flow_id, runtime.roster.keys, ports.now()
        )
    else:
        contribution = bake_contribution(run, effects, ports.now())
    record_contribution_once(store, contribution)
    binding = serving_binding_for(store, grant, run)
    if binding is None:
        raise InvariantError(BAKE_FAN_OUT_FAILED, "a finalizable cohort has no binding.")
    execution = store.read_serving_execution(grant, run.run_id)
    if execution is None:
        raise InvariantError(BAKE_FAN_OUT_FAILED, "a finalizable cohort has no serving authority.")
    authority = store.read_serving_domain(execution.head.domain)
    if authority.head != inputs.head:
        raise InvariantError(BAKE_FAN_OUT_FAILED, "a finalizable cohort's head changed.")
    # ADR-139: pages move only with a binding presentation accepts, so CURRENT stays last-good.
    pointer_items = (
        tuple(
            pointer_transaction_items(
                runtime.pointer_table, _advance_from(cast("dict[str, object]", extra["pointer"]))
            )
            for effect, extra in parsed
            if effect.effect_kind is BakeEffectKind.WRITTEN
        )
        if binding_authorizes_serving(binding)
        else ()
    )
    receipt = bake_v3_digest(
        _FINALIZED,
        [
            run.run_id,
            grant,
            inputs.head.version,
            [effect.effect_digest for effect in effects],
            binding.binding_id,
            precompute.settled_at.isoformat(),
        ],
    )
    record = json.dumps(
        {
            "schema_version": "3",
            "node": BAKE_FINALIZE_NODE,
            "receipt_digest": receipt,
            "effects": [effect.effect_digest for effect in effects],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return BakeFinalizeRequest(
        run=run,
        claim=inputs.claim,
        node=inputs.node,
        tasks=settled,
        effects=effects,
        pointer_items=pointer_items,
        precompute=precompute,
        binding=binding,
        head=inputs.head,
        receipt_digest=receipt,
        effect_record=record,
        recorded_at=ports.now().isoformat(),
        readiness_digest=readiness.digest,
    )


@dataclass(slots=True)
class _FinalizeObservation:
    """The finalizer's measured members and wait, for its `bake.finalize` span."""

    members: int | None = None
    wait_ms: int | None = None
    serving_authorized: bool | None = None
    """Whether the committed binding authorizes serving (ADR-139); unset when none was read."""


@trace_boundary(BAKE_FINALIZE)
def consume_bake_finalize(
    ports: BakePorts, delivery: BakeDelivery, runtime: BakeRuntime
) -> ConsumeResult:
    """Run the finalizer inside its `bake.finalize` span (plan 4.7, A2.1)."""
    sent = delivery.body
    cohort = sent.owner["cohort_revision"]
    observation = _FinalizeObservation()
    with (
        correlated(sent.run_id, sent.node_id, cohort),
        span(
            BAKE_FINALIZE,
            run_id=sent.run_id,
            task_id=sent.node_id,
            cohort_revision=cohort,
            task_count=len(runtime.roster.keys),
        ) as recording,
    ):
        result = _consume_finalize(ports, delivery, runtime, observation)
        recording.record(
            members=observation.members,
            wait_ms=observation.wait_ms,
            serving_authorized=observation.serving_authorized,
            outcome=result.outcome,
        )
        return result


def _consume_finalize(  # noqa: PLR0911, PLR0912, PLR0913, PLR0917 -- one ordered finalize lifecycle
    ports: BakePorts,
    delivery: BakeDelivery,
    runtime: BakeRuntime,
    observation: _FinalizeObservation,
) -> ConsumeResult:
    """Claim the finalizer and commit the winner's one final transaction (DB7 under A3).

    A redelivery after a committed finalize is refused at the claim (`graph_already_settled`) or
    finds its node terminal; both re-signal the committed binding (`_resignal_settled`), so a crash
    between the commit and its `GraphServingWithheld` emission is repaired by the next delivery.
    """
    claim, refused = _claim(ports, delivery)
    if claim is None:
        if refused.outcome == "graph_already_settled":
            unread = _resignal_settled(ports, runtime, delivery.body, observation)
            if unread is not None:
                return unread
        return refused
    store, body = ports.store, delivery.body
    run = store.read_run(body.run_id)
    node = _node(store, body.run_id, body.node_id)
    if run is None or node is None:
        return ConsumeResult(True, "graph_stale_execution_attempt")
    if node.state in TERMINAL_NODE_STATES:
        store.release_finalize_runtime(node)
        unread = _resignal_settled(ports, runtime, body, observation)
        if unread is not None:
            return unread
        return ConsumeResult(True, "graph_already_settled", node.state)
    head = _live_head(store, run)
    if head is None or run.state is not RunState.GRANTED:
        return ConsumeResult(True, "graph_stale_execution_attempt")
    observation.wait_ms = _wait_ms(node.armed_at, ports.now())
    inputs = _FinalizeInputs(run, node, claim, head)
    try:
        built = _finalize_request(ports, inputs, runtime)
        if isinstance(built, _Abandon):
            # Routed with the lease held: the abandon settles the node under this claim.
            return _abandon_finalize(ports, delivery, runtime, (inputs, built))
        if isinstance(built, ConsumeResult):
            release_claim_lease(store, claim, ports.now())
            if built.outcome == PRECOMPUTE_ARMED:
                _shorten_armed_redelivery(ports, delivery, runtime)
            return built
        outcome = store.finalize_bake(built)
    except GraphGuardError:
        release_claim_lease(store, claim, ports.now())
        return ConsumeResult(False, "graph_bake_finalize_conflict", NodeState.CLAIMED)
    except Exception as error:
        _report(ports, delivery, error)
        if not delivery.final or isinstance(error, DependencyError):
            release_claim_lease(store, claim, ports.now())
            return ConsumeResult(False, "graph_bake_finalize_retryable", NodeState.CLAIMED)
        # F5: a deterministic error repeats on every receive, so the last one settles the cohort
        # withheld instead of dead-lettering; earlier receives retry, as before.
        abandon = _Abandon("finalize_error", _fatal_code(error))
        return _abandon_finalize(ports, delivery, runtime, (inputs, abandon))
    finalized = store.read_nodes(run.run_id)
    settled = next((item for item in finalized if item.node_id == node.node_id), node)
    store.release_finalize_runtime(settled)
    observation.members = (
        _FINAL_AUTHORITY_MEMBERS
        + len(built.effects)
        + sum(len(items) for items in built.pointer_items)
    )
    runtime.log("finalize", outcome.value, run_id=run.run_id, members=observation.members)
    observation.serving_authorized = binding_authorizes_serving(built.binding)
    _signal_serving(
        runtime,
        run.run_id,
        built.binding,
        fresh_commit=outcome is BakeFinalizeOutcome.FINALIZED,
    )
    return ConsumeResult(True, f"graph_bake_{outcome.value}", NodeState.SUCCEEDED)


def _abandon_finalize(  # noqa: PLR0913, PLR0917 -- the ports, delivery, runtime and the abandon
    ports: BakePorts,
    delivery: BakeDelivery,
    runtime: BakeRuntime,
    abandon: tuple[_FinalizeInputs, _Abandon],
) -> ConsumeResult:
    """Settle the finalizer FAILED with the cohort withheld (ADR-140, B4/F5); never raise.

    A failure is reported and retried from stored state, as any finalizer error: every step is
    idempotent and the node is settled last.
    """
    inputs, _decided = abandon
    try:
        return _abandoned(ports, runtime, abandon)
    except Exception as error:
        _report(ports, delivery, error)
        release_claim_lease(ports.store, inputs.claim, ports.now())
        return ConsumeResult(False, "graph_bake_finalize_retryable", NodeState.CLAIMED)


def _abandoned(
    ports: BakePorts, runtime: BakeRuntime, abandon: tuple[_FinalizeInputs, _Abandon]
) -> ConsumeResult:
    """Record the withheld bake member, bind stale, and settle the finalizer FAILED (§2.2).

    1. The bake contribution is read first: a stored one (recorded before a later error) is
       kept, since contributions are write-once; else the withheld one is recorded.
    2. A stored binding is used; else a derivable one is bound `stale` through `bind_once`
       (non-authorizing and withheld). A stored or adopted binding that authorizes serving was
       bound by precompute from the real bake member: settling FAILED would complete the run
       under it while no pointer moved, so the abandon is refused and the receive retries.
    3. The node settles FAILED `post_grant_invariant` under the held claim.
    4. A bound binding is signalled; one WARNING `finalize abandoned` names the cause.
    """
    inputs, decided = abandon
    store, run = ports.store, inputs.run
    grant = cast("str", run.grant_id)
    stored = next(
        (item for item in store.read_contributions(grant) if item.member is CohortMember.BAKE),
        None,
    )
    if stored is None:
        record_contribution_once(
            store,
            withheld_bake_contribution(grant, run.root_flow_id, runtime.roster.keys, ports.now()),
        )
    binding = store.read_serving_binding(grant)
    if binding is None:
        derived = serving_binding_for(store, grant, run)
        if derived is not None:
            binding = bind_once(store, replace(derived, stale=True))
    if binding is not None and binding_authorizes_serving(binding):
        runtime.log(
            "finalize",
            "abandon_refused",
            LogSeverity.WARNING,
            run_id=run.run_id,
            cause=decided.cause,
            reason="authorizing_binding",
        )
        release_claim_lease(store, inputs.claim, ports.now())
        return ConsumeResult(False, "graph_bake_finalize_retryable", NodeState.CLAIMED)
    try:
        store.settle_node(
            NodeSettlement(
                run,
                inputs.node,
                NodeState.FAILED,
                ports.now().isoformat(),
                claim=inputs.claim,
                failure_reason=POST_GRANT_INVARIANT,
                failure_code=decided.code,
            )
        )
    except GraphGuardError:
        return ConsumeResult(True, "graph_claim_lost")
    store.release_finalize_runtime(inputs.node)
    if binding is not None:
        _signal_serving(runtime, run.run_id, binding, fresh_commit=False)
    runtime.log(
        "finalize",
        "abandoned",
        LogSeverity.WARNING,
        run_id=run.run_id,
        cause=decided.cause,
        failure_code=decided.code,
        bake_outcome=(CohortOutcome.UNAVAILABLE if stored is None else stored.outcome).value,
        bound=binding is not None,
    )
    return ConsumeResult(True, "graph_bake_finalize_abandoned", NodeState.FAILED, decided.code)


def _signal_serving(  # noqa: PLR0913 -- the runtime, the committed binding and its commit kind
    runtime: BakeRuntime, run_id: str, binding: ServingBinding, *, fresh_commit: bool
) -> None:
    """Signal a committed binding's serving state from durable state (ADR-139).

    A withheld binding (`serving_withheld`: a failure kept its pages on last-good) emits
    `GraphServingWithheld` = 1 and one WARNING naming its failure reasons, on every outcome that
    reads it, so the signal is emitted at least once. A non-withheld binding emits 0 only on a
    fresh `FINALIZED` commit, the newest bound cohort of the one ingestion release domain; a
    replay may be older than a withheld cohort, so it never clears the alarm.
    """
    if serving_withheld(binding):
        runtime.telemetry(lambda: runtime.serving_signal(1))
        runtime.log(
            "finalize",
            "serving_withheld",
            LogSeverity.WARNING,
            run_id=run_id,
            failure_reasons=sorted({reason for _key, reason in failure_entries(binding)}),
            unavailable_count=len(binding.unavailable),
            binding_kind=None if binding.binding_kind is None else binding.binding_kind.value,
            execution_attempts=binding.execution_attempts,
            stale=binding.stale,
        )
    elif fresh_commit:
        runtime.telemetry(lambda: runtime.serving_signal(0))


def _resignal_settled(  # noqa: PLR0913, PLR0917 -- the ports, runtime, delivery and span
    ports: BakePorts,
    runtime: BakeRuntime,
    body: BakeV3Body,
    observation: _FinalizeObservation,
) -> ConsumeResult | None:
    """Re-signal a settled finalizer from its stored binding; a failed read is retried.

    Returns `None` when the stored state was read (signalled, or nothing to signal because the
    finalizer did not succeed or the run has no grant). A read failure, or a succeeded, granted
    finalizer whose committed binding reads as no item, is logged WARNING
    `finalize serving_signal_unread` and returns the unacknowledged
    `graph_bake_signal_retryable`, so the redelivery repeats the read; the bake dead letter's
    `BakeDeadLettered` pages when the redrive bound runs out.
    """
    store = ports.store
    node_read = False
    try:
        run = store.read_run(body.run_id)
        node = _node(store, body.run_id, body.node_id)
        node_read = node is not None
        if (
            run is None
            or node is None
            or node.state is not NodeState.SUCCEEDED
            or run.grant_id is None
        ):
            return None
        binding = store.read_serving_binding(run.grant_id)
    # error-propagation: logged WARNING below and returned unacknowledged for redelivery
    except Exception as error:
        cause = type(error).__name__
    else:
        if binding is not None:
            observation.serving_authorized = binding_authorizes_serving(binding)
            _signal_serving(runtime, body.run_id, binding, fresh_commit=False)
            return None
        # The final transaction commits the node and its binding together, so a succeeded,
        # granted finalizer with no binding is unreadable committed state: retry, never acknowledge.
        cause = "binding_missing"
    runtime.log(
        "finalize",
        "serving_signal_unread",
        LogSeverity.WARNING,
        run_id=body.run_id,
        cause=cause,
    )
    return ConsumeResult(
        False, "graph_bake_signal_retryable", NodeState.SUCCEEDED if node_read else None
    )


def _shorten_armed_redelivery(
    ports: BakePorts, delivery: BakeDelivery, runtime: BakeRuntime
) -> None:
    """Redeliver the armed finalizer after `FINALIZE_ARMED_REDELIVERY_SECONDS`, not 960 s.

    The same override `graph_materialize` applies to a transient refusal. Availability only: the
    lease is already released and the message stays unacknowledged, so when the override is not
    applied the queue's visibility applies, which is the behaviour without it; that case is
    logged WARNING with its cause, never silent. A failure here never changes the result.
    """
    seconds = FINALIZE_ARMED_REDELIVERY_SECONDS
    cause = "no_visibility_target"
    if ports.visibility is not None and delivery.queue_url and delivery.receipt_handle:
        try:
            ports.visibility(delivery.queue_url, delivery.receipt_handle, seconds)
        # error-propagation: availability only; logged WARNING below, queue visibility applies
        except Exception as failure:
            cause = type(failure).__name__
        else:
            runtime.log(
                "finalize",
                "graph_bake_finalize_redelivery",
                run_id=delivery.body.run_id,
                task_id=delivery.body.node_id,
                visibility_seconds=seconds,
            )
            return
    runtime.log(
        "finalize",
        "graph_bake_finalize_redelivery_unshortened",
        LogSeverity.WARNING,
        run_id=delivery.body.run_id,
        task_id=delivery.body.node_id,
        cause=cause,
    )


def _wait_ms(armed_at: str | None, now: datetime) -> int | None:
    """A2's `finalize_wait`: from the finalizer's arming (every task terminal) to its claim."""
    if armed_at is None:
        return None
    return max(0, int((now - datetime.fromisoformat(armed_at)).total_seconds() * 1_000))


@dataclass(frozen=True, slots=True)
class GraphBakeRuntime:
    """The Baker's composed V3 graph bake: fan-out, task and finalize consumers."""

    store: ReleaseStore
    runtime: BakeRuntime
    now: Callable[[], datetime]
    logger: Logger | None = None
    invariant_metric: Callable[[str], None] = emit_post_grant_invariant
    visibility: Callable[[str, str, int], None] | None = None
    """The bake queue's visibility override, for the finalizer's armed nack."""
    coordinator: GraphBakeCoordinator = field(init=False)
    tasks: GraphBakeTaskWork = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "coordinator", GraphBakeCoordinator(self.runtime))
        object.__setattr__(self, "tasks", GraphBakeTaskWork(self.runtime))

    def consume(
        self, envelope: PostGrantEnvelope, remaining_ms: Callable[[], int]
    ) -> ConsumeResult:
        """Consume one fan-out control through the shared POST_GRANT protocol."""
        return PostGrantRuntime(self.store, self.coordinator, self.now, self.logger).consume(
            envelope, remaining_ms
        )

    def consume_v3(self, delivery: BakeDelivery, remaining_ms: Callable[[], int]) -> ConsumeResult:
        """Consume one task or finalize message; a task's deadline derives from `remaining_ms`."""
        ports = BakePorts(self.store, self.now, self.logger, self.invariant_metric, self.visibility)
        if delivery.body.kind == "bake_finalize":
            return consume_bake_finalize(ports, delivery, self.runtime)
        return consume_bake_task(ports, delivery, self.tasks, remaining_ms)
