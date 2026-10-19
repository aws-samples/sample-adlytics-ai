# SPDX-License-Identifier: MIT-0
"""ADR-075 shared inline-run boundary: run persistence, §5 statements, and lease-held finalization.

Every port the driver uses is a hand-rolled recording stub over one small authority
world; no AWS SDK call, emulator, or patching participates. Every identity below is
fictional fixture data.
"""

from __future__ import annotations

# The readback oracle is the storage adapter's private §2.2 step-5 renderer.
# pyright: reportPrivateUsage=false
import hashlib
import pathlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from aws_lambda_powertools import Logger
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.inbound.ingestion_handlers import inline_run
from adapters.inbound.ingestion_handlers.inline_run import (
    InlineRunOutcome,
    InlineRunRequest,
    InlineRunRuntime,
    ServablePointerTarget,
    run_inline_table,
)
from adapters.outbound.athena.semantic_storage import (
    UnitWorkload,
    _expected_records,
    canonical_unit_records,
    ordered_records,
)
from adapters.outbound.athena.table_attempt_statements import (
    ComposeUnitStatementsRequest,
    compose_unit_statements,
)
from adapters.outbound.s3.semantic_storage import canonical_object_bytes, projection_unit_source
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import TABLE_INLINE_RUN
from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog
from domain.operations.projection_handoff import canonical_json
from domain.operations.stage1_lifecycle_head import Stage1LifecycleHeadTuple
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    OwnerDisposition,
    derive_inline_identity,
)
from domain.operations.statement_protocol import StatementStartBounds
from domain.operations.status import MEASUREMENT_REFS
from domain.operations.table_attempt import (
    AttemptObservation,
    BindingDigestRequest,
    FenceObservation,
    ProviderErrorClass,
    ProviderTerminalState,
    ReconciliationObservation,
    RunIdentityRequest,
    SanitizedStartEvidence,
    WriterKind,
    binding_digest,
    run_identity,
)
from domain.operations.table_lease import TableLeaseRecord, inline_lease_holder
from domain.semantics.canonical_compiler import CanonicalRow, canonical_rows_checksum
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationId,
    PublicationLensKey,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.time import (
    BusinessCalendarKey,
    FactTimeClass,
    MeasurementBucket,
    TimeGrain,
)
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination, CanonicalMaterializeRequest
from ports.outbound.projection_coordinator import (
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    AcquireTableFenceResult,
    BeginInlineRunOwner,
    BeginTableAttemptOutcome,
    BeginTableAttemptRequest,
    BeginTableAttemptResult,
    CompleteTableAttemptOutcome,
    CompleteTableAttemptRequest,
    CompleteTableAttemptResult,
    FinalizeInlineOutcome,
    FinalizeInlineRequest,
    FinalizeInlineResult,
    InlineRunAttemptOwner,
    ParkAttemptOutcome,
    ParkAttemptRequest,
    ParkAttemptResult,
    PersistInlineRunOutcome,
    PersistInlineRunRequest,
    PersistInlineRunResult,
    PointerOutcomeKind,
    RecordStage1RefusalOutcome,
    RecordStage1RefusalRequest,
    RecordStage1RefusalResult,
    ReleaseTableFenceOutcome,
    ReleaseTableFenceRequest,
    ReleaseTableFenceResult,
    RequestAttemptCancellationOutcome,
    RequestAttemptCancellationRequest,
    RequestAttemptCancellationResult,
    Stage1RefusalReason,
    Stage1RefusalTag,
)
from ports.outbound.table_attempt import (
    RecordSubmittedAttemptRequest,
    RecordSubmittedAttemptResult,
)
from ports.outbound.table_attempt_start import (
    Accepted,
    StartTableAttemptRequest,
    StartTableAttemptResult,
)
from ports.outbound.table_attempt_terminal import (
    ObserveTableAttemptResult,
    ProviderNonterminal,
    ProviderTerminalObservation,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# --- Fictional fixture identities -----------------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json")
NOW = datetime(2026, 9, 19, 12, 34, 56, 789012, tzinfo=UTC)
DAY = date(2026, 9, 19)
DATASET = "fct_retail_purchase_amc"
CURRENT_DATABASE = "fictional-current"
TABLE_ID = f"{CURRENT_DATABASE}.{DATASET}"
PUBLICATION_ID = "publication-fictional-0001"
FENCING_TOKEN = 7
CALENDAR = BusinessCalendarKey("iso_gregorian", 0)
WRITER = WriterKind.INGEST_DELIVERY_OPERATOR
OPERATION_ID = "operation-fictional-0001"
EXECUTION_ID = "ab" * 32
RUN_ID = run_identity(RunIdentityRequest(WRITER, OPERATION_ID, EXECUTION_ID, TABLE_ID))
RUN_FLOW = derive_inline_identity(
    InlineIdentityRequest(WRITER.value, OPERATION_ID, TABLE_ID, EXECUTION_ID)
).flow_id
FULL_BUDGET_MS = 900_000
SAFETY_DEADLINE_MS = 840_000
BOUNDS = StatementStartBounds(
    max_statement_bytes=200_000,
    max_encoded_identity_bytes=128,
)
LOGGER = Logger(service="adlytics-inline-run-test")


def _canonical_request() -> CanonicalMaterializeRequest:
    rows: tuple[CanonicalRow, ...] = (
        (
            ("amazon_asin", "amazon_asin:b0fictional01"),
            ("basis_key", "retail_actual"),
            ("currency_code", "USD"),
            ("date_key", DAY),
            ("distinct_user_count", 100),
            ("ecosystem_key", "amazon"),
            ("marketplace_key", "amazon_us"),
            ("platform_key", "amazon_retail"),
            ("revenue_purchased", Decimal("12.3400")),
            ("rows_suppressed", False),
            ("source_timezone", "UTC"),
            ("suppression_reason", None),
            ("units_purchased", 10),
        ),
    )
    checksum = canonical_rows_checksum(rows)
    partition = PartitionIdentity(
        PublicationScope(
            CanonicalDatasetKey(DATASET),
            CommerceScopeKey("fx"),
            "us",
            PublicationLensKey("lens"),
            FactTimeClass.DAILY_ADDITIVE,
            CALENDAR,
            "UTC",
        ),
        MeasurementBucket(DateRange(DAY, DAY), TimeGrain.DAY, CALENDAR, "UTC"),
    )
    revision = SourceRevision.instant(
        scheme=RevisionSchemeKey("amc.execution"),
        source_id="execution-fictional",
        observed_at=NOW,
    )
    source = SourcePublicationRef("source-fictional-0001")
    subject = PublicationEvidenceSubject(
        "canonical-curation",
        partition.identity,
        (source.value,),
        "amc.execution|instant|2026-09-19T12:34:56.789012+00:00|execution-fictional",
        checksum,
    )
    results = tuple(
        PrepublicationRuleResult(
            family, f"fictional.{family.value}.v1", RuleOutcome.PASSED, "Fictional rule passed."
        )
        for family in PrepublicationRuleFamily
    )
    successful = SuccessfulPublication(
        publication_id=PublicationId(PUBLICATION_ID),
        publisher=CanonicalPublisherKey("canonical-curation"),
        partition=partition,
        attempt_id=AttemptId("attempt-fictional-0001"),
        source_inputs=(source,),
        source_revision=revision,
        content_checksum=checksum,
        evidence=PublicationEvidence("semantic-v2.1", subject, results),
        fencing_token=FENCING_TOKEN,
        published_at=NOW,
    )
    return CanonicalMaterializeRequest(
        CanonicalDestination.SUCCESSFUL, successful, rows, "semantic-v2.1"
    )


CANONICAL = _canonical_request()
PARTITION_IDENTITY = CANONICAL.publication.partition.identity
TABLE = CATALOG.canonical(DATASET, successful=True)
RECORDS = canonical_unit_records(projection_unit_source(canonical_object_bytes(CANONICAL)), TABLE)
WORKLOAD = UnitWorkload(TABLE, CURRENT_DATABASE, ((PUBLICATION_ID, RECORDS),))
COMPOSITION = compose_unit_statements(
    ComposeUnitStatementsRequest(workload=WORKLOAD, workgroup="fictional-batch")
)
EXPECTED_READBACK = tuple(_expected_records(TABLE, ordered_records(TABLE, RECORDS)))
SERVABLE = ServablePointerTarget(PARTITION_IDENTITY, PUBLICATION_ID, FENCING_TOKEN)


# --- The authority world and its recording stubs --------------------------------------------------


def _current_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PARTITION_IDENTITY,
        "record_key": "CURRENT",
        "publication_id": PUBLICATION_ID,
        "fencing_token": FENCING_TOKEN,
    }
    item.update(overrides)
    return item


def _pending(binding: BindingDigestRequest) -> ReconciliationObservation:
    digest = binding_digest(binding)
    return ReconciliationObservation(
        reconcile_status="pending",
        reconcile_after_or_block=f"{1_789_821_296_789_012 + 930_000_000:020d}#{digest}",
        reconcile_attempt_count=0,
    )


@dataclass
class _World:
    """The durable facts the driver strongly reads; stubs apply the coordinator's effects."""

    run_item: dict[str, object] | None = None
    head: Stage1LifecycleHeadTuple | None = None
    lease: TableLeaseRecord | None = None
    fence: FenceObservation | None = None
    attempts: dict[str, AttemptObservation] = field(default_factory=dict[str, AttemptObservation])
    current: dict[str, dict[str, object]] = field(
        default_factory=lambda: {PARTITION_IDENTITY: _current_item()}
    )


class _Reads:
    def __init__(self, world: _World) -> None:
        self.world = world

    def run_item(self, table_id: str, run_id: str) -> dict[str, object] | None:
        assert (table_id, run_id) == (TABLE_ID, RUN_ID)
        return None if self.world.run_item is None else dict(self.world.run_item)

    def head(self, flow_id: str) -> Stage1LifecycleHeadTuple | None:
        assert flow_id == RUN_FLOW
        return self.world.head

    def flow_root_clock(self, flow_id: str) -> datetime:
        assert flow_id == RUN_FLOW
        return NOW

    def lease(self, table_id: str) -> TableLeaseRecord | None:
        assert table_id == TABLE_ID
        return self.world.lease

    def fence(self, table_id: str) -> FenceObservation | None:
        assert table_id == TABLE_ID
        return self.world.fence

    def attempt(self, table_id: str, attempt_id: str) -> AttemptObservation | None:
        assert table_id == TABLE_ID
        return self.world.attempts.get(attempt_id)

    def current_item(self, partition_identity: str) -> dict[str, object] | None:
        item = self.world.current.get(partition_identity)
        return None if item is None else dict(item)


@dataclass
class _Coordinator:
    """Records every typed request and applies the durable effect the transaction would."""

    world: _World
    calls: list[str]
    acquire_outcome: AcquireTableFenceOutcome = AcquireTableFenceOutcome.ACQUIRED
    requests: list[object] = field(default_factory=list[object])

    def _record(self, name: str, request: object) -> None:
        self.calls.append(name)
        self.requests.append(request)

    def _run(self) -> dict[str, object]:
        item = self.world.run_item
        assert item is not None
        return item

    def persist_inline_run(self, request: PersistInlineRunRequest) -> PersistInlineRunResult:
        self._record("persist_inline_run", request)
        run_id = request.run_id()
        if self.world.run_item is not None:
            return PersistInlineRunResult(PersistInlineRunOutcome.REPLAYED, run_id)
        self.world.run_item = {
            "partition_key": f"TABLE_ATTEMPT#{TABLE_ID}",
            "record_key": f"RUN#{run_id}",
            "run_id": run_id,
            "writer_kind": request.writer_kind.value,
            "operation_id": request.operation_id,
            "execution_id": request.execution_id,
            "physical_table_id": request.physical_table_id,
            "status_flow_binding": request.proof.flow_id,
            "execution_binding": request.proof.execution_id,
            "table_kind": request.table_kind,
            "statement_intent_kinds": list(request.statement_intent_kinds),
            "created_at": request.created_at,
            "state": "open",
            "next_ordinal": 1,
            "accepted_attempt_reservations": 0,
            "attempt_references": [],
        }
        self.world.head = request.proof.resulting_head
        return PersistInlineRunResult(PersistInlineRunOutcome.PERSISTED, run_id)

    def acquire_table_fence(self, request: AcquireTableFenceRequest) -> AcquireTableFenceResult:
        self._record("acquire_table_fence", request)
        if self.acquire_outcome is not AcquireTableFenceOutcome.ACQUIRED:
            return AcquireTableFenceResult(self.acquire_outcome)
        self.world.lease = TableLeaseRecord(
            TABLE_ID, request.holder, request.new_fencing_token, request.new_expires_at
        )
        return AcquireTableFenceResult(
            AcquireTableFenceOutcome.ACQUIRED,
            fencing_token=request.new_fencing_token,
            expires_at=request.new_expires_at,
        )

    def release_table_fence(self, request: ReleaseTableFenceRequest) -> ReleaseTableFenceResult:
        self._record("release_table_fence", request)
        return ReleaseTableFenceResult(ReleaseTableFenceOutcome.RELEASED)

    def record_stage1_refusal(
        self, request: RecordStage1RefusalRequest
    ) -> RecordStage1RefusalResult:
        self._record("record_stage1_refusal", request)
        self.world.head = request.proof.resulting_head
        return RecordStage1RefusalResult(RecordStage1RefusalOutcome.RECORDED)

    def begin_table_attempt(self, request: BeginTableAttemptRequest) -> BeginTableAttemptResult:
        self._record("begin_table_attempt", request)
        identity = request.identity()
        attempt_id = identity.attempt_id
        lease = request.lease
        run = self._run()
        run.update(
            {
                "current_attempt_id": attempt_id,
                "lease_holder": lease.holder,
                "fencing_token": lease.fencing_token,
                "reserved_slot_identity": request.reservation_identity,
                "accepted_attempt_reservations": int(
                    str(run.get("accepted_attempt_reservations", 0))
                )
                + 1,
            }
        )
        self.world.attempts[attempt_id] = AttemptObservation(
            partition_key=f"TABLE_ATTEMPT#{TABLE_ID}",
            record_key=f"ATTEMPT#{attempt_id}",
            state="intent",
            physical_table_id=TABLE_ID,
            attempt_id=attempt_id,
            plan_unit_owner_ref=None,
            inline_run_owner_ref=str(run["run_id"]),
            ordinal=request.ordinal,
            statement_kind=request.statement_kind,
            lease_holder=lease.holder,
            fencing_token=lease.fencing_token,
            writer_execution_id=request.writer_execution_id,
            start_envelope=request.start_envelope,
            client_request_token=identity.client_request_token,
            envelope_digest=identity.envelope_digest,
            reservation_identity=request.reservation_identity,
        )
        self.world.fence = FenceObservation(
            partition_key=f"TABLE_ATTEMPT#{TABLE_ID}",
            record_key="FENCE",
            state="active",
            physical_table_id=TABLE_ID,
            attempt_id=attempt_id,
            lease_holder=lease.holder,
            fencing_token=lease.fencing_token,
            parked_at=None,
            reconciliation=_pending(
                BindingDigestRequest(TABLE_ID, attempt_id, lease.holder, lease.fencing_token)
            ),
        )
        return BeginTableAttemptResult(
            BeginTableAttemptOutcome.BEGUN,
            attempt_id,
            identity.client_request_token,
            identity.envelope_digest,
            request.ordinal,
        )

    def request_attempt_cancellation(
        self, request: RequestAttemptCancellationRequest
    ) -> RequestAttemptCancellationResult:
        self._record("request_attempt_cancellation", request)
        attempt_id = str(request.attempt.attempt_id)
        self.world.attempts[attempt_id] = replace(
            self.world.attempts[attempt_id],
            state="cancel_requested",
            cancel_requested_at=request.cancel_requested_at,
        )
        return RequestAttemptCancellationResult(RequestAttemptCancellationOutcome.REQUESTED)

    def park_attempt(self, request: ParkAttemptRequest) -> ParkAttemptResult:
        self._record("park_attempt", request)
        attempt_id = str(request.attempt.attempt_id)
        self.world.attempts[attempt_id] = replace(
            self.world.attempts[attempt_id], state="parked", parked_at=request.park_instant
        )
        self.world.fence = replace(request.fence, state="parked", parked_at=request.park_instant)
        self.world.lease = replace(request.lease, parked_at=request.park_instant)
        return ParkAttemptResult(ParkAttemptOutcome.PARKED)

    def complete_table_attempt(
        self, request: CompleteTableAttemptRequest
    ) -> CompleteTableAttemptResult:
        self._record("complete_table_attempt", request)
        attempt_id = str(request.attempt.attempt_id)
        self.world.attempts[attempt_id] = replace(
            self.world.attempts[attempt_id],
            state=request.provider_state.value,
            query_count=request.query_count,
            scanned_bytes=request.scanned_bytes,
            engine_ms=request.engine_ms,
            terminal_at=request.terminal_at,
            athena_usage_state="unsettled",
        )
        self.world.fence = None
        run = self._run()
        references = cast("list[object]", run.get("attempt_references", []))
        run["attempt_references"] = [*references, attempt_id]
        run["next_ordinal"] = int(str(run["next_ordinal"])) + 1
        del run["current_attempt_id"]
        return CompleteTableAttemptResult(CompleteTableAttemptOutcome.COMPLETED)

    def finalize_inline(self, request: FinalizeInlineRequest) -> FinalizeInlineResult:
        self._record("finalize_inline", request)
        run = self._run()
        run["state"] = "terminal"
        for member in request.settlement.attempts:
            attempt_id = str(member.attempt.attempt_id)
            self.world.attempts[attempt_id] = replace(
                self.world.attempts[attempt_id], athena_usage_state="settled"
            )
        self.world.head = request.proof.resulting_head
        return FinalizeInlineResult(FinalizeInlineOutcome.FINALIZED)


@dataclass
class _Attempts:
    world: _World
    calls: list[str]

    def record_submitted_attempt(
        self, request: RecordSubmittedAttemptRequest
    ) -> RecordSubmittedAttemptResult:
        self.calls.append("record_submitted_attempt")
        self.world.attempts[request.attempt_id] = replace(
            self.world.attempts[request.attempt_id],
            state="submitted",
            query_execution_id=request.query_execution_id,
            submitted_at=request.submitted_at,
            sanitized_start_evidence=request.sanitized_start_evidence,
        )
        return RecordSubmittedAttemptResult.RECORDED


@dataclass
class _Start:
    calls: list[str]
    outcomes: list[StartTableAttemptResult]

    def start_table_attempt(self, request: StartTableAttemptRequest) -> StartTableAttemptResult:
        del request
        self.calls.append("start")
        return self.outcomes.pop(0)


def _accepted(query_execution_id: str) -> Accepted:
    return Accepted(query_execution_id, SanitizedStartEvidence(200, ProviderErrorClass.NONE))


def _terminal(
    state: ProviderTerminalState = ProviderTerminalState.SUCCEEDED,
) -> ProviderTerminalObservation:
    return ProviderTerminalObservation(
        state=state, query_count=1, scanned_bytes=2048, engine_ms=350
    )


@dataclass
class _Clock:
    """Monotonic milliseconds owned by the test; sleeping advances it, never the wall."""

    now_ms: int = 0
    slept: list[int] = field(default_factory=list[int])

    def monotonic_ms(self) -> int:
        return self.now_ms

    def sleep_ms(self, duration_ms: int) -> None:
        assert duration_ms >= 1
        self.slept.append(duration_ms)
        self.now_ms += duration_ms

    def remaining_budget_ms(self) -> int:
        return max(0, FULL_BUDGET_MS - self.now_ms)


@dataclass
class _Terminals:
    calls: list[str]
    terminal: ProviderTerminalObservation = field(default_factory=_terminal)
    readback: tuple[tuple[str | None, ...], ...] = EXPECTED_READBACK
    nonterminal_until_stop: bool = False
    never_terminal: bool = False
    stopped: list[str] = field(default_factory=list[str])

    def observe(self, query_execution_id: str) -> ObserveTableAttemptResult:
        self.calls.append("observe")
        if self.never_terminal or (
            self.nonterminal_until_stop and query_execution_id not in self.stopped
        ):
            return ProviderNonterminal(reason="nonterminal")
        return self.terminal

    def stop(self, query_execution_id: str) -> None:
        self.calls.append("stop")
        self.stopped.append(query_execution_id)
        self.terminal = _terminal(ProviderTerminalState.CANCELLED)

    def readback_rows(self, query_execution_id: str) -> tuple[tuple[str | None, ...], ...]:
        del query_execution_id
        self.calls.append("readback")
        return self.readback


@dataclass
class _Harness:
    world: _World
    coordinator: _Coordinator
    terminals: _Terminals
    clock: _Clock
    calls: list[str]
    runtime: InlineRunRuntime

    def run(self, **overrides: object) -> Any:
        return run_inline_table(self.runtime, _request(self.clock, **overrides))

    def request_of(self, kind: type[Any]) -> Any:
        matches = [item for item in self.coordinator.requests if isinstance(item, kind)]
        assert len(matches) == 1, f"expected exactly one {kind.__name__}, saw {len(matches)}"
        return matches[0]


def _request(clock: _Clock, **overrides: object) -> InlineRunRequest:
    fields: dict[str, object] = {
        "writer_kind": WRITER,
        "operation_id": OPERATION_ID,
        "execution_id": EXECUTION_ID,
        "physical_table_id": TABLE_ID,
        "table_kind": "current",
        "workload": WORKLOAD,
        "composition": COMPOSITION,
        "remaining_budget_ms": clock.remaining_budget_ms,
        "servable": (SERVABLE,),
    }
    fields.update(overrides)
    return InlineRunRequest(**fields)  # type: ignore[arg-type]


def _harness(
    world: _World | None = None,
    *,
    starts: tuple[StartTableAttemptResult, ...] = (_accepted("q-1"), _accepted("q-2")),
    acquire: AcquireTableFenceOutcome = AcquireTableFenceOutcome.ACQUIRED,
) -> _Harness:
    calls: list[str] = []
    selected = world or _World()
    clock = _Clock()
    coordinator = _Coordinator(selected, calls, acquire_outcome=acquire)
    terminals = _Terminals(calls)
    runtime = InlineRunRuntime(
        coordinator=cast("Any", coordinator),
        attempts=cast("Any", _Attempts(selected, calls)),
        start=cast("Any", _Start(calls, list(starts))),
        terminals=cast("Any", terminals),
        reads=_Reads(selected),
        bounds=BOUNDS,
        lease_duration_seconds=930,
        wall=lambda: NOW,
        monotonic_ms=clock.monotonic_ms,
        sleep_ms=clock.sleep_ms,
        logger=LOGGER,
    )
    return _Harness(selected, coordinator, terminals, clock, calls, runtime)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    return exporter


def _run_span(exporter: InMemorySpanExporter) -> dict[str, object]:
    (found,) = [item for item in exporter.get_finished_spans() if item.name == TABLE_INLINE_RUN]
    return dict(found.attributes or {})


# --- Ownership and identity -----------------------------------------------------------------------


def test_run_inline_table_is_the_one_table_inline_run_boundary_owner() -> None:
    assert run_inline_table.__trace_boundaries__ == (TABLE_INLINE_RUN,)  # type: ignore[attr-defined]
    owners = [
        name
        for name in dir(inline_run)
        if hasattr(getattr(inline_run, name), "__trace_boundaries__")
    ]
    assert owners == ["run_inline_table"]


def test_inline_lease_holder_is_the_pinned_five_member_constructor() -> None:
    """The holder must reproduce the write-discipline §3.2 tuple, not a queued-shaped one."""
    holder = inline_lease_holder(RunIdentityRequest(WRITER, OPERATION_ID, EXECUTION_ID, TABLE_ID))
    expected = hashlib.sha256(
        canonical_json(["inline", WRITER.value, OPERATION_ID, EXECUTION_ID, TABLE_ID])
    ).hexdigest()
    assert holder == expected
    assert len(holder) == 64
    other = inline_lease_holder(
        RunIdentityRequest(WriterKind.TRANSITIONAL_DELIVERY, OPERATION_ID, EXECUTION_ID, TABLE_ID)
    )
    assert other != holder


def test_request_rejects_a_workload_for_another_table_and_duplicate_pointer_targets() -> None:
    clock = _Clock()
    with pytest.raises(ValueError, match="database-qualified table"):
        _request(clock, physical_table_id="fictional-current.other_table")
    with pytest.raises(ValueError, match="distinct partitions"):
        _request(clock, servable=(SERVABLE, SERVABLE))


# --- The clean run --------------------------------------------------------------------------------


def test_clean_run_persists_leases_drives_two_statements_finalizes_and_releases(
    exporter: InMemorySpanExporter,
) -> None:
    harness = _harness()

    result = harness.run()

    assert result.outcome == InlineRunOutcome.FINALIZED
    assert result.retryable is False
    assert result.run_id == RUN_ID
    persist = harness.request_of(PersistInlineRunRequest)
    established_head = persist.proof.resulting_head
    assert established_head is not None
    assert (
        result.flow_id,
        result.execution_id,
        result.predecessor_execution_id,
    ) == (
        persist.proof.flow_id,
        persist.proof.execution_id,
        established_head.predecessor_execution_id,
    )
    assert harness.calls == [
        "persist_inline_run",
        "acquire_table_fence",
        "begin_table_attempt",
        "start",
        "record_submitted_attempt",
        "observe",
        "complete_table_attempt",
        "begin_table_attempt",
        "start",
        "record_submitted_attempt",
        "observe",
        "complete_table_attempt",
        "readback",
        "finalize_inline",
        "release_table_fence",
    ]
    assert harness.world.run_item is not None
    assert harness.world.run_item["state"] == "terminal"
    attributes = _run_span(exporter)
    assert attributes == {
        "flow_id": RUN_FLOW,
        "causation_event_id": attributes["causation_event_id"],
        "execution_id": attributes["execution_id"],
        "internal_operation_id": OPERATION_ID,
        "writer_kind": WRITER.value,
        "table_kind": "current",
        "stage": "run",
        "completed": 2,
        "total": 2,
        "outcome": "finalized",
    }
    # A legitimate root has no parent flow; `None` is dropped rather than recorded.
    assert "parent_flow_id" not in attributes
    assert attributes["execution_id"] != EXECUTION_ID  # public UUID, never the internal id


def test_persist_carries_only_the_closed_writer_kind_roster_and_recorded_root_proof() -> None:
    harness = _harness()
    harness.run()

    persist = harness.request_of(PersistInlineRunRequest)
    assert persist.writer_kind is WRITER
    assert persist.statement_intent_kinds == ("merge", "verification")
    assert persist.table_kind == "current"
    assert persist.pointer_finalization_payload is None
    assert persist.proof.prior_head is None
    assert persist.proof.flow_id == RUN_FLOW
    assert (
        persist.proof.events[0].reattest.authority.transition_id == "semantic_inline_run_recorded"
    )


def test_statements_begin_against_the_fresh_run_owner_with_its_execution_identity() -> None:
    harness = _harness()
    harness.run()

    begins = [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    assert [request.ordinal for request in begins] == [1, 2]
    assert all(isinstance(request.owner, BeginInlineRunOwner) for request in begins)
    assert all(request.owner_ref == RUN_ID for request in begins)
    assert all(request.writer_execution_id == EXECUTION_ID for request in begins)
    assert all(request.lease.holder == harness.world.lease.holder for request in begins)  # type: ignore[union-attr]
    completes = [
        r for r in harness.coordinator.requests if isinstance(r, CompleteTableAttemptRequest)
    ]
    assert all(isinstance(request.owner, InlineRunAttemptOwner) for request in completes)


def test_finalization_settles_under_the_inline_measurement_and_advances_the_pointer() -> None:
    harness = _harness()
    harness.run()

    finalize = harness.request_of(FinalizeInlineRequest)
    assert finalize.run.run_id == RUN_ID
    assert finalize.observed_fence is None
    assert finalize.settlement.owner_disposition is OwnerDisposition.SUCCESSFUL
    assert len(finalize.settlement.attempts) == 2  # grain: one settlement per accepted attempt
    for member in finalize.settlement.attempts:
        settlement = member.settlement
        assert settlement.flow_id == RUN_FLOW
        assert (
            settlement.evaluation_binding.measurement_refs
            == (MEASUREMENT_REFS["semantic_inline_table_run"])
        )
        assert settlement.scanned_bytes == 2048  # bytes, as the provider terminal reported
    (pointer,) = finalize.pointers
    assert pointer.kind is PointerOutcomeKind.ADVANCED
    assert pointer.partition_identity == PARTITION_IDENTITY
    assert pointer.projected_fencing_token == FENCING_TOKEN
    assert (
        pointer.proof.events[0].reattest.authority.transition_id == "semantic_projection_servable"
    )
    assert (
        finalize.proof.events[0].reattest.authority.transition_id == "semantic_inline_run_terminal"
    )
    assert all(
        observation.athena_usage_state == "settled"
        for observation in harness.world.attempts.values()
    )


def test_a_newer_projected_pointer_is_omitted_as_superseded_not_failed() -> None:
    world = _World()
    world.current[PARTITION_IDENTITY] = _current_item(
        projected_publication_id="publication-fictional-0002",
        projected_fencing_token=FENCING_TOKEN + 1,
    )
    harness = _harness(world)

    result = harness.run()

    assert result.outcome == InlineRunOutcome.FINALIZED
    (pointer,) = harness.request_of(FinalizeInlineRequest).pointers
    assert pointer.kind is PointerOutcomeKind.PROJECTION_SUPERSEDED
    assert pointer.observed_current is None


def test_source_and_candidate_runs_take_the_lease_but_finalize_with_no_pointers() -> None:
    harness = _harness()

    result = harness.run(table_kind="candidate", servable=())

    assert result.outcome == InlineRunOutcome.FINALIZED
    assert "acquire_table_fence" in harness.calls
    assert harness.request_of(FinalizeInlineRequest).pointers == ()


def test_exact_replay_reuses_the_persisted_run_identity_and_head(
    exporter: InMemorySpanExporter,
) -> None:
    harness = _harness(starts=tuple(_accepted(f"q-{index}") for index in range(1, 5)))
    first = harness.run()
    persisted_head = harness.world.head
    # A second delivery of the same run: the RUN# item and head already exist.
    harness.world.run_item["state"] = "open"  # type: ignore[index]
    harness.world.run_item["attempt_references"] = []  # type: ignore[index]
    harness.calls.clear()

    second = harness.run()

    assert harness.calls[0] == "persist_inline_run"
    persists = [r for r in harness.coordinator.requests if isinstance(r, PersistInlineRunRequest)]
    assert persists[1].proof.prior_head == persisted_head
    second_head = persists[1].proof.resulting_head
    assert second_head is not None
    assert (second.flow_id, second.execution_id, second.predecessor_execution_id) == (
        persists[1].proof.flow_id,
        persists[1].proof.execution_id,
        second_head.predecessor_execution_id,
    )
    assert second.flow_id == first.flow_id
    spans = [item for item in exporter.get_finished_spans() if item.name == TABLE_INLINE_RUN]
    assert len(spans) == 2
    assert {dict(s.attributes or {})["flow_id"] for s in spans} == {RUN_FLOW}


# --- Refusals -------------------------------------------------------------------------------------


def test_an_active_fence_records_the_inline_tagged_pending_refusal_without_a_lease(
    exporter: InMemorySpanExporter,
) -> None:
    world = _World()
    world.fence = FenceObservation(
        partition_key=f"TABLE_ATTEMPT#{TABLE_ID}",
        record_key="FENCE",
        state="active",
        physical_table_id=TABLE_ID,
        attempt_id="a1" * 32,
        lease_holder="holder-fictional-other",
        fencing_token=3,
        parked_at=None,
        reconciliation=_pending(
            BindingDigestRequest(TABLE_ID, "a1" * 32, "holder-fictional-other", 3)
        ),
    )
    harness = _harness(world)

    result = harness.run()

    assert result.outcome == InlineRunOutcome.REFUSED_TABLE_ATTEMPT_PENDING
    assert result.retryable is True
    assert harness.calls == ["persist_inline_run", "record_stage1_refusal"]
    refusal = harness.request_of(RecordStage1RefusalRequest)
    assert refusal.tag is Stage1RefusalTag.INLINE
    assert refusal.reason is Stage1RefusalReason.TABLE_ATTEMPT_PENDING
    assert refusal.unit is None
    assert refusal.inline is not None
    assert refusal.inline.run.run_id == RUN_ID
    assert refusal.proof.events[0].reattest.authority.transition_id == "semantic_inline_run_refused"
    assert world.lease is None
    assert _run_span(exporter)["outcome"] == "refused_table_attempt_pending"
    assert _run_span(exporter)["completed"] == 0


def test_a_busy_table_records_the_inline_busy_refusal_and_stays_retryable() -> None:
    harness = _harness(acquire=AcquireTableFenceOutcome.TABLE_BUSY)

    result = harness.run()

    assert result.outcome == InlineRunOutcome.REFUSED_TABLE_BUSY
    assert result.retryable is True
    assert harness.calls == ["persist_inline_run", "acquire_table_fence", "record_stage1_refusal"]
    assert harness.request_of(RecordStage1RefusalRequest).reason is Stage1RefusalReason.TABLE_BUSY
    assert "release_table_fence" not in harness.calls


# --- Statement dispositions -----------------------------------------------------------------------


def test_safety_deadline_parks_the_attempt_and_leaves_the_run_open_without_release(
    exporter: InMemorySpanExporter,
) -> None:
    harness = _harness()
    harness.terminals.never_terminal = True  # Stop succeeds but the provider never reports terminal

    result = harness.run()

    assert result.outcome == InlineRunOutcome.ATTEMPT_PARKED
    assert result.retryable is True
    assert harness.clock.now_ms >= SAFETY_DEADLINE_MS  # milliseconds of monotonic budget
    assert "request_attempt_cancellation" in harness.calls
    assert harness.calls.count("stop") == 1
    assert "park_attempt" in harness.calls
    assert "finalize_inline" not in harness.calls
    assert "release_table_fence" not in harness.calls  # only the reconciler clears the pair
    assert harness.world.run_item is not None
    assert harness.world.run_item["state"] == "open"
    assert _run_span(exporter)["outcome"] == "attempt_parked"


def test_a_failed_provider_terminal_is_non_retryable_and_releases_the_lease(
    exporter: InMemorySpanExporter,
) -> None:
    harness = _harness()
    harness.terminals.terminal = _terminal(ProviderTerminalState.FAILED)

    result = harness.run()

    assert result.outcome == InlineRunOutcome.STATEMENT_FAILED
    assert result.retryable is False
    assert "complete_table_attempt" in harness.calls  # exact attempt accounting closes first
    assert "finalize_inline" not in harness.calls
    assert harness.calls[-1] == "release_table_fence"
    attributes = _run_span(exporter)
    assert attributes["outcome"] == "statement_failed"
    assert attributes["error_type"] == "_StatementFailedError"


def test_a_verification_readback_mismatch_is_non_retryable_and_never_finalizes() -> None:
    harness = _harness()
    harness.terminals.readback = (("unexpected",) * len(EXPECTED_READBACK[0]),)

    result = harness.run()

    assert result.outcome == InlineRunOutcome.VERIFICATION_MISMATCH
    assert result.retryable is False
    assert "finalize_inline" not in harness.calls
    assert harness.calls[-1] == "release_table_fence"


def test_insufficient_budget_before_a_statement_is_retryable_and_releases_the_lease() -> None:
    harness = _harness()
    harness.clock.now_ms = FULL_BUDGET_MS - 1_000  # 1 second left: below the §5 minimum

    result = harness.run()

    assert result.outcome == InlineRunOutcome.INSUFFICIENT_BUDGET
    assert result.retryable is True
    assert "begin_table_attempt" not in harness.calls  # no reservation, no Athena call
    assert harness.calls[-1] == "release_table_fence"


def test_cancellation_at_q_persists_the_request_stops_once_and_closes_the_cancelled_attempt() -> (
    None
):
    harness = _harness()
    harness.terminals.nonterminal_until_stop = True

    result = harness.run()

    assert result.outcome == InlineRunOutcome.STATEMENT_FAILED
    assert harness.calls.count("request_attempt_cancellation") == 1
    assert harness.calls.count("stop") == 1
    assert harness.calls.count("complete_table_attempt") == 1
    cancellation = harness.request_of(RequestAttemptCancellationRequest)
    assert isinstance(cancellation.owner, InlineRunAttemptOwner)
    assert cancellation.owner.run_id == RUN_ID
    complete = harness.request_of(CompleteTableAttemptRequest)
    assert complete.provider_state is ProviderTerminalState.CANCELLED


# --- S6 §2.5: the graph-unit inline writer (D5) ---------------------------------------------------

GRAPH_RUN = "r" * 64
GRAPH_NODE = "n" * 64


def _graph_fences() -> tuple[Any, ...]:
    return (_graph_fence(),)


def _graph_fence() -> Any:
    from ports.outbound.projection_coordinator import GraphClaimFence  # noqa: PLC0415

    return GraphClaimFence(GRAPH_NODE, 3, {"pk": f"EXEC_CLAIM#{GRAPH_NODE}", "lease_version": 3})


class _AnyRunReads(_Reads):
    """The graph unit's run identity derives from its own writer kind, run and node."""

    def run_item(self, table_id: str, run_id: str) -> dict[str, object] | None:
        del table_id, run_id
        return None if self.world.run_item is None else dict(self.world.run_item)

    def head(self, flow_id: str) -> Stage1LifecycleHeadTuple | None:
        del flow_id
        return self.world.head

    def flow_root_clock(self, flow_id: str) -> datetime:
        del flow_id
        return NOW


def _graph_harness(world: _World | None = None) -> _Harness:
    harness = _harness(world, starts=tuple(_accepted(f"q-{index}") for index in range(1, 5)))
    harness.runtime = replace(harness.runtime, reads=_AnyRunReads(harness.world))
    return harness


def _graph_run(
    harness: _Harness, fences: list[object] | None = None, *, claims: tuple[Any, ...] | None = None
) -> Any:
    seen = fences if fences is not None else []

    def fence() -> Any:
        seen.append(1)
        return (_graph_fence(),) if claims is None else claims

    return run_inline_table(
        harness.runtime,
        _request(
            harness.clock,
            writer_kind=WriterKind.GRAPH_UNIT,
            operation_id=GRAPH_RUN,
            execution_id=GRAPH_NODE,
            servable=(),
            graph_claims=fence,
        ),
    )


def test_s6_d5_every_writer_kind_has_exactly_its_legitimate_root() -> None:
    from domain.operations.stage1_operational_status import (  # noqa: PLC0415
        RootDiscriminator,
        Stage1OwnerTransition,
    )

    assert set(inline_run._ROOT_BY_WRITER) == set(WriterKind)  # pyright: ignore[reportPrivateUsage]
    assert inline_run._ROOT_BY_WRITER[WriterKind.GRAPH_UNIT] == (  # pyright: ignore[reportPrivateUsage]
        Stage1OwnerTransition.FIRST_GRAPH_UNIT_INLINE_RECORDED,
        RootDiscriminator.GRAPH_UNIT_INLINE,
    )


def test_s6_a_graph_unit_run_finalizes_with_no_pointer_and_fences_every_begin() -> None:
    harness = _graph_harness()
    fences: list[object] = []
    result = _graph_run(harness, fences)
    assert result.outcome == InlineRunOutcome.FINALIZED
    persist = harness.request_of(PersistInlineRunRequest)
    assert persist.writer_kind is WriterKind.GRAPH_UNIT
    assert (persist.operation_id, persist.execution_id) == (GRAPH_RUN, GRAPH_NODE)
    begins = [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    assert len(begins) == 2
    assert all(request.graph_claims == (_graph_fence(),) for request in begins)
    assert len(fences) == 2  # the claim item is strongly re-read for each Begin
    assert harness.request_of(FinalizeInlineRequest).pointers == ()


def test_s6_control_a_legacy_inline_writer_begins_without_a_graph_fence() -> None:
    harness = _harness()
    harness.run()
    begins = [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    assert begins and all(request.graph_claims == () for request in begins)


def test_s6_a_graph_unit_at_the_reservation_cap_is_final_with_no_begin() -> None:
    harness = _graph_harness()
    _graph_run(harness)  # persist the run, then exhaust its permanent reservations
    assert harness.world.run_item is not None
    harness.world.run_item.update(
        {"state": "open", "accepted_attempt_reservations": 10, "next_ordinal": 11}
    )
    harness.coordinator.requests.clear()
    result = _graph_run(harness)
    assert (result.outcome, result.retryable) == (InlineRunOutcome.ATTEMPT_BUDGET_EXHAUSTED, False)
    assert not [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]


def test_s6_control_a_graph_unit_below_the_cap_begins() -> None:
    harness = _graph_harness()
    _graph_run(harness)
    assert harness.world.run_item is not None
    harness.world.run_item.update(
        {"state": "open", "accepted_attempt_reservations": 9, "next_ordinal": 10}
    )
    harness.coordinator.requests.clear()
    _graph_run(harness)
    assert [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]


def test_s6_a_graph_unit_request_carries_exactly_its_fence_and_no_pointer() -> None:
    clock = _Clock()
    with pytest.raises(ValueError, match="exactly a graph-unit run carries the graph claim fence"):
        _request(clock, writer_kind=WriterKind.GRAPH_UNIT, servable=())
    with pytest.raises(ValueError, match="exactly a graph-unit run carries the graph claim fence"):
        _request(clock, graph_claims=_graph_fences)
    with pytest.raises(ValueError, match="owns no servable pointers"):
        _request(clock, writer_kind=WriterKind.GRAPH_UNIT, graph_claims=_graph_fences)


# --- Slice 7 (1b): fence arity (12f) and typed deadline cancellation (1d) -------------------------


def test_slice7_12f_a_graph_unit_without_a_thunk_or_a_legacy_run_with_one_is_refused() -> None:
    clock = _Clock()
    with pytest.raises(ValueError, match="exactly a graph-unit run carries the graph claim fence"):
        _request(clock, writer_kind=WriterKind.GRAPH_UNIT, servable=(), graph_claims=None)
    with pytest.raises(ValueError, match="exactly a graph-unit run carries the graph claim fence"):
        _request(clock, graph_claims=_graph_fences)
    # Controls: a non-graph request without a thunk, and a graph-unit request with one, build.
    assert _request(clock).graph_claims is None
    graph = _request(
        clock, writer_kind=WriterKind.GRAPH_UNIT, servable=(), graph_claims=_graph_fences
    )
    assert graph.graph_claims is _graph_fences


def test_slice7_12f_an_empty_graph_thunk_is_refused_before_any_begin() -> None:
    from domain.shared_kernel.errors import (  # noqa: PLC0415
        GRAPH_UNIT_EMPTY_CLAIMS_CODE,
        InvariantError,
    )

    harness = _graph_harness()
    with pytest.raises(InvariantError) as refused:
        _graph_run(harness, claims=())
    assert refused.value.code == GRAPH_UNIT_EMPTY_CLAIMS_CODE
    begins = [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    assert begins == []
    # Control: a thunk returning one fence begins with exactly that fence.
    control = _graph_harness()
    assert _graph_run(control).outcome == InlineRunOutcome.FINALIZED
    begun = [r for r in control.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    assert begun and all(len(request.graph_claims) == 1 for request in begun)


def test_slice7_1d_a_deadline_cancelled_failure_is_typed_on_the_result() -> None:
    harness = _harness()
    harness.terminals.nonterminal_until_stop = True
    result = harness.run()
    assert (result.outcome, result.deadline_cancelled) == (InlineRunOutcome.STATEMENT_FAILED, True)
    # Control: a plain provider failure before the cancellation deadline reads false.
    control = _harness()
    control.terminals.terminal = _terminal(ProviderTerminalState.FAILED)
    failed = control.run()
    assert (failed.outcome, failed.deadline_cancelled) == (InlineRunOutcome.STATEMENT_FAILED, False)
    # Control: a finalized run reads false.
    assert _harness().run().deadline_cancelled is False
