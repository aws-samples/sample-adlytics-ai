# SPDX-License-Identifier: MIT-0
"""ADR-075 phase-2 table writer: §4 receive identity, §2.2 unit lifecycle, and §5 protocol.

Every port the handler drives is a hand-rolled recording stub over one small
authority world; no AWS SDK call, emulator, or patching participates. Every
identity below is fictional fixture data.
"""

from __future__ import annotations

# This module exercises the handler adapter's private orchestration seams.
# pyright: reportPrivateUsage=false
import importlib
import json
import pathlib
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast

import pytest
from unit_staging_support import RecordingStager, workload_verification_rows

from adapters import config
from adapters.config import settings_without_client_secret, tracing_settings
from adapters.outbound.athena.semantic_storage import AthenaSemanticTarget
from adapters.outbound.dynamodb.execution_graph_dispatch import batch_dispatch_id
from adapters.outbound.dynamodb.projection_coordinator import _plan_meta_item
from adapters.outbound.s3.semantic_storage import canonical_object_bytes, canonical_object_key
from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog
from domain.operations.execution_graph import AuthorityMode, ModeState
from domain.operations.execution_graph_dispatch import execution_set_id_for
from domain.operations.stage1_lifecycle_head import (
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    derive_stage1_lifecycle_proof,
)
from domain.operations.stage1_operational_status import (
    LegitimateRootSource,
    OwnerDisposition,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    RootDiscriminator,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_plan_identity,
    derive_unit_identity,
)
from domain.operations.stage1_operational_status import (
    RequiredChildExpectation as StatusChildExpectation,
)
from domain.operations.status import FlowState, canonical_authority_digest
from domain.operations.table_attempt import (
    AttemptObservation,
    BindingDigestRequest,
    FenceObservation,
    ProviderErrorClass,
    ProviderTerminalState,
    ReconciliationObservation,
    SanitizedStartEvidence,
    binding_digest,
)
from domain.operations.table_lease import TableLeaseRecord, queued_lease_holder
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    RequiredChildExpectation,
    SettledAttemptReference,
    SuccessorOwner,
    plan_partition_key,
    plan_settled_attempt_digest,
    roster_digest,
    source_delivery_identity,
)
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
from domain.shared_kernel.errors import DependencyError, DomainError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination, CanonicalMaterializeRequest
from ports.outbound.projection_coordinator import (
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    AcquireTableFenceResult,
    BeginTableAttemptOutcome,
    BeginTableAttemptRequest,
    BeginTableAttemptResult,
    ClaimUnitOutcome,
    ClaimUnitRequest,
    ClaimUnitResult,
    CompletePlanOutcome,
    CompletePlanRequest,
    CompletePlanResult,
    CompleteTableAttemptOutcome,
    CompleteTableAttemptRequest,
    CompleteTableAttemptResult,
    FailUnitOutcome,
    FailUnitRequest,
    FailUnitResult,
    FinalizeUnitOutcome,
    FinalizeUnitRequest,
    FinalizeUnitResult,
    ParkAttemptOutcome,
    ParkAttemptRequest,
    ParkAttemptResult,
    PointerOutcomeKind,
    RecordStage1RefusalRequest,
    ReleaseTableFenceOutcome,
    ReleaseTableFenceRequest,
    ReleaseTableFenceResult,
    RequestAttemptCancellationOutcome,
    RequestAttemptCancellationRequest,
    RequestAttemptCancellationResult,
    Stage1RefusalReason,
    Stage1RefusalTag,
    UnitFailureReason,
)
from ports.outbound.table_attempt import (
    RecordSubmittedAttemptRequest,
    RecordSubmittedAttemptResult,
)
from ports.outbound.table_attempt_start import (
    AcceptanceAmbiguous,
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
NOW_MICROS = 1_789_821_296_789_012
DAY = date(2026, 9, 19)
DATASET = "fct_retail_purchase_amc"
CURRENT_DATABASE = "fictional-current"
UNIT_KEY = f"{CURRENT_DATABASE}.{DATASET}"
PLAN_DIGEST = "2" * 64
EXEC64 = "12" * 32
PLAN_KEY = plan_partition_key(PLAN_DIGEST, EXEC64)
INTENT_ID = "ab" * 32
ORIGIN = IntentOrigin(
    IntentOriginKind.SOURCE_DELIVERY,
    source_delivery_identity((("manifests/delivery-fictional.json", "v-0001"),)),
)
MESSAGE_ID = "message-fictional-0001"
RECEIPT_HANDLE = "receipt-fictional-0001"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
WRITER_EXEC_1 = "89f75fc1261f9d5db4997efaed880653711034a7d8794331c3fc64de420e2bcf"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
WRITER_EXEC_2 = "4f1c996c60ed0df4fe1437d0d825eebabd57709ea844407dda0f2621b7e4ee56"
PUBLICATION_ID = "publication-fictional-0001"
FENCING_TOKEN = 7
CALENDAR = BusinessCalendarKey("iso_gregorian", 0)
FULL_BUDGET_MS = 900_000
SAFETY_DEADLINE_MS = 840_000
CANCELLATION_DEADLINE_MS = 780_000
# §5 step-4 cadence: 250 ms, then a 500 ms cap (5 s until 2026-10-10).
POLL_MS = 500
POLL_RAMP_MS = (250,)
MAX_RECEIVES = 3
PLAN_RESERVATION_CAP = 32
USAGE_DIGEST = "c" * 64

_INGESTION_ENV = {
    "ADL_PROJECTION_HANDOFF_STATE": "QUEUED_ACTIVE",
    "ADL_STAGE1_MAX_UNITS": "15",
    "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT": "10",
    "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN": "32",
    "ADL_STAGE1_MAX_GROUP_MEMBERS": "4",
    "ADL_STAGE1_MAX_POINTERS_PER_TABLE": "37",
    "ADL_STAGE1_MAX_OBJECT_REFS_PER_UNIT": "37",
    "ADL_STAGE1_MAX_ROWS_PER_UNIT": "50000",
    "ADL_STAGE1_MAX_ROWS_PER_DELIVERY": "20000",
    "ADL_STAGE1_ATHENA_MAX_STATEMENT_BYTES": "200000",
    "ADL_STAGE1_MAX_ENCODED_IDENTITY_BYTES": "128",
    "ADL_STAGE1_MAX_PARTITION_IDENTITY_BYTES": "256",
    "ADL_TABLE_WRITER_TIMEOUT_SECONDS": "900",
    "ADL_TABLE_WRITER_CLEANUP_RESERVE_MS": "60000",
    "ADL_TABLE_WRITER_CONCURRENCY": "8",
    "ADL_TABLE_WRITER_MAX_RECEIVE_COUNT": str(MAX_RECEIVES),
    "ADL_TABLE_LEASE_DURATION_SECONDS": "930",
    "ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS": "60",
    "ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE": "10",
    "ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS": "60",
    "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
    "ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE": "50",
    "ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS": "60",
    "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
    "ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS": "300",
    "ADL_SEMANTIC_WRITE_QUEUE_URL": "https://sqs.test.invalid/semantic-write.fifo",
    "ADL_EXECUTION_GRAPH_TABLE": "fictional-execution-graph",
    "ADL_RAW_BUCKET": "fictional-raw",
}
_SETTINGS_ENV = {
    "ADL_ENVIRONMENT": "test",
    "ADL_AWS_REGION": "us-east-1",
    "ADL_COGNITO_USER_POOL_ID": "unused-by-writer",
    "ADL_COGNITO_CLIENT_ID": "unused-by-writer",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
    "ADL_APP_BASE_URL": "https://app.test.invalid",
    "ADL_API_BASE_URL": "https://api.test.invalid",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
    "ADL_ATHENA_WORKGROUP": "fictional-interactive",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "fictional-attempts",
    "ADL_SEMANTIC_LEASES_TABLE": "fictional-leases",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "fictional-publications",
    "ADL_SEMANTIC_V2_BUCKET": "fictional-semantic",
    "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP": "fictional-bake",
    "ADL_SEMANTIC_V2_SOURCE_DATABASE": "fictional-source",
    "ADL_SEMANTIC_V2_CANDIDATE_DATABASE": "fictional-candidate",
    "ADL_SEMANTIC_V2_CURRENT_DATABASE": CURRENT_DATABASE,
    "ADL_SEMANTIC_V2_STAGING_DATABASE": "fictional_staging",
    "ADL_SEMANTIC_V2_STAGING_BUCKET": "fictional-staging",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "2",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "unit-test-not-a-credential",
    # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
    "AWS_SECRET_ACCESS_KEY": "unit-test-not-a-secret",
    "AWS_EC2_METADATA_DISABLED": "true",
}


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name, value in {**_SETTINGS_ENV, **_INGESTION_ENV}.items():
        monkeypatch.setenv(name, value)
    caches = (
        settings_without_client_secret,
        tracing_settings,
        config.table_writer_settings,
        config._queued_ingestion_config,
        config.run_request_api_settings,
    )
    for accessor in caches:
        accessor.cache_clear()
    module_name = "adapters.inbound.ingestion_handlers.table_writer"
    sys.modules.pop(module_name, None)
    loaded = importlib.import_module(module_name)
    yield loaded
    sys.modules.pop(module_name, None)
    for accessor in caches:
        accessor.cache_clear()


@dataclass
class GraphModeStub:
    """The S3 per-batch mode read: SHADOW unless a test sets another mode (V-12)."""

    mode: ModeState = field(default_factory=lambda: ModeState(AuthorityMode.SHADOW, 1))
    reads: int = 0

    def read_mode(self) -> ModeState:
        self.reads += 1
        return self.mode

    def read_mode_item(self) -> dict[str, object]:
        return {"pk": "MODE#ingestion", "sk": "MODE", "mode": self.mode.mode.value, "revision": 1}


# --- Immutable canonical authority ----------------------------------------------------------------


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
    # Short scope/marketplace/lens members keep the partition identity within the
    # §11 128-byte encoded-identity cap the roster entry enforces.
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
OBJECT_BYTES = canonical_object_bytes(CANONICAL)
PARTITION_IDENTITY = CANONICAL.publication.partition.identity
CHECKSUM_HEX = CANONICAL.publication.content_checksum.removeprefix("sha256:")
OBJECT_REFERENCE = canonical_object_key(
    CanonicalDestination.SUCCESSFUL.value, DATASET, PUBLICATION_ID
)


def _unit_entry(checksum: str = CHECKSUM_HEX) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=UNIT_KEY,
        table_kind="current",
        merge_count=1,
        objects=(
            CanonicalObjectEntry(
                kind="canonical",
                reference=OBJECT_REFERENCE,
                content_checksum=checksum,
                publication_id=PUBLICATION_ID,
                partition_key=PARTITION_IDENTITY,
                fencing_token=FENCING_TOKEN,
            ),
        ),
    )


UNIT = _unit_entry()
ROSTER_DIGEST = roster_digest((UNIT,))
UNIT_FLOW = derive_unit_identity(UnitIdentityRequest(PLAN_DIGEST, UNIT_KEY, WRITER_EXEC_1)).flow_id
PLAN_FLOW = derive_plan_identity(PlanIdentityRequest(PLAN_DIGEST, EXEC64)).flow_id
INSTANT = f"{NOW_MICROS:020d}"


# --- Plan-side authority -------------------------------------------------------------------------


def _plan_meta(**overrides: object) -> PlanMeta:
    fields: dict[str, object] = {
        "plan_digest": PLAN_DIGEST,
        "internal_execution_id": EXEC64,
        "root_plan_key": PLAN_KEY,
        "generation": 0,
        "source_plan_key": None,
        "origin_intent_id": INTENT_ID,
        "roster_digest": ROSTER_DIGEST,
        "origin": ORIGIN,
        "status": PlanStatus.PLANNED,
        "unit_count": 1,
        "admitted_count": 1,
        "attested_count": 0,
        "failed_count": 0,
        "child_outcome_reduction_version": 2,
        "required_child_expectations": (RequiredChildExpectation(UNIT_FLOW, 0),),
        "settled_attempt_count": 0,
        "settled_attempt_digest": None,
        "terminal_at": None,
        "created_at": INSTANT,
    }
    fields.update(overrides)
    return PlanMeta(**fields)  # type: ignore[arg-type]


def _intent() -> IntentMeta:
    return IntentMeta(
        root_plan_key=None,
        source_plan_key=None,
        plan_digest=PLAN_DIGEST,
        intent_id=INTENT_ID,
        roster_digest=ROSTER_DIGEST,
        origin=ORIGIN,
        state=IntentState.ADMITTED,
        claimed_generation=0,
        retry_count=0,
        due_at=INSTANT,
        ready_status=None,
        ready_order=None,
        created_at=INSTANT,
        last_transition_at=INSTANT,
    )


def _owner(execution_id: str = EXEC64, generation: int = 0) -> SuccessorOwner:
    """Build the successor owner whose active plan is the last persisted plan."""
    active = plan_partition_key(PLAN_DIGEST, execution_id)
    return SuccessorOwner(
        revision=2,
        root_plan_key=PLAN_KEY,
        last_plan_key=active,
        last_generation=generation,
        active_plan_key=active,
        active_intent_id=INTENT_ID,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=execution_id,
    )


def _plan_head() -> Stage1LifecycleHeadTuple:
    proof: Stage1LifecycleProof = derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PlanIdentityRequest(PLAN_DIGEST, EXEC64),
            internal_key=(PLAN_KEY,),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_plan_recorded", FlowState.QUEUED, None, None
            ),
            stage="projection_plan_recorded",
            authority_at=NOW,
            prior_head=None,
            required_child_expectations=PlanRequiredChildExpectations(
                PLAN_FLOW, (StatusChildExpectation(UNIT_FLOW, 0),)
            ),
        )
    )
    head = proof.resulting_head
    assert head is not None
    return head


def _unit_item(status: str = "queued", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "partition_key": PLAN_KEY,
        "record_key": f"UNIT#{UNIT_KEY}",
        "flow_id": UNIT_FLOW,
        "owner_generation": 0,
        "status": status,
        "admission": "admitted",
        "created_at": INSTANT,
        "unit_key": UNIT.unit_key,
        "table_kind": UNIT.table_kind,
        "merge_count": UNIT.merge_count,
        "objects": [entry.to_member() for entry in UNIT.objects],
    }
    item.update(overrides)
    return item


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
        reconcile_after_or_block=f"{NOW_MICROS + 930_000_000:020d}#{digest}",
        reconcile_attempt_count=0,
    )


# --- The authority world and its recording stubs --------------------------------------------------


@dataclass
class _World:
    """The durable facts the writer strongly reads; stubs apply the coordinator's effects."""

    owner: SuccessorOwner | None = field(default_factory=_owner)
    intent: IntentMeta | None = field(default_factory=_intent)
    plan_meta: PlanMeta | None = field(default_factory=_plan_meta)
    unit_item: dict[str, object] | None = field(default_factory=_unit_item)
    plan_head: Stage1LifecycleHeadTuple | None = field(default_factory=_plan_head)
    unit_head: Stage1LifecycleHeadTuple | None = None
    current: dict[str, dict[str, object]] = field(
        default_factory=lambda: {PARTITION_IDENTITY: _current_item()}
    )
    lease: TableLeaseRecord | None = None
    fence: FenceObservation | None = None
    attempts: dict[str, AttemptObservation] = field(default_factory=dict[str, AttemptObservation])
    plan_reservation_count: int = 0

    def plan_meta_item(self) -> dict[str, object] | None:
        if self.plan_meta is None:
            return None
        item = dict(_plan_meta_item(PLAN_KEY, self.plan_meta))
        if self.plan_reservation_count:
            item["plan_reservation_count"] = self.plan_reservation_count
        return item


class _Reader:
    def __init__(self, world: _World) -> None:
        self.world = world

    def owner(self, plan_digest: str) -> SuccessorOwner | None:
        assert plan_digest == PLAN_DIGEST
        return self.world.owner

    def intent(self, plan_digest: str, intent_id: str) -> IntentMeta | None:
        assert (plan_digest, intent_id) == (PLAN_DIGEST, INTENT_ID)
        return self.world.intent

    def plan_meta(self, plan_key: str) -> PlanMeta | None:
        assert plan_key == PLAN_KEY
        return self.world.plan_meta

    def plan_meta_item(self, plan_key: str) -> dict[str, object] | None:
        assert plan_key == PLAN_KEY
        return self.world.plan_meta_item()

    def unit_item(self, plan_key: str, unit_key: str) -> dict[str, object] | None:
        assert (plan_key, unit_key) == (PLAN_KEY, UNIT_KEY)
        return None if self.world.unit_item is None else dict(self.world.unit_item)

    def plan_unit_items(self, plan_key: str) -> tuple[dict[str, object], ...]:
        assert plan_key == PLAN_KEY
        return () if self.world.unit_item is None else (dict(self.world.unit_item),)

    def head(self, flow_id: str) -> Stage1LifecycleHeadTuple | None:
        if flow_id == PLAN_FLOW:
            return self.world.plan_head
        if flow_id == UNIT_FLOW:
            return self.world.unit_head
        raise AssertionError(f"unexpected head read {flow_id}")

    def flow_root_clock(self, flow_id: str) -> datetime:
        assert flow_id in (PLAN_FLOW, UNIT_FLOW)
        return NOW

    def current_item(self, partition_identity: str) -> dict[str, object] | None:
        item = self.world.current.get(partition_identity)
        return None if item is None else dict(item)

    def lease(self, table_id: str) -> TableLeaseRecord | None:
        assert table_id == UNIT_KEY
        return self.world.lease

    def fence(self, table_id: str) -> FenceObservation | None:
        assert table_id == UNIT_KEY
        return self.world.fence

    def attempt(self, table_id: str, attempt_id: str) -> AttemptObservation | None:
        assert table_id == UNIT_KEY
        return self.world.attempts.get(attempt_id)

    def attempt_item(self, table_id: str, attempt_id: str) -> dict[str, object] | None:
        observation = self.attempt(table_id, attempt_id)
        if observation is None:
            return None
        return {
            "state": observation.state,
            "athena_usage_state": observation.athena_usage_state,
            "usage_authority_digest": USAGE_DIGEST,
        }


@dataclass
class _Coordinator:
    """Records every typed request and applies the durable effect the transaction would."""

    world: _World
    calls: list[str]
    acquire_outcome: AcquireTableFenceOutcome = AcquireTableFenceOutcome.ACQUIRED
    held_until: str | None = None
    prior_writer_live_until: str | None = None
    finalize_conflicts: int = 0
    requests: list[object] = field(default_factory=list[object])
    drain_reads: list[AuthorityMode] = field(default_factory=list[AuthorityMode])

    def count_drain(self, observed: AuthorityMode) -> None:
        """S6 (V-17b): the batch's mode read, recorded before any transaction."""
        self.drain_reads.append(observed)

    def _record(self, name: str, request: object) -> None:
        self.calls.append(name)
        self.requests.append(request)

    def _unit(self) -> dict[str, object]:
        item = self.world.unit_item
        assert item is not None
        return item

    def claim_unit(self, request: ClaimUnitRequest) -> ClaimUnitResult:
        self._record("claim_unit", request)
        if request.reclaim and self.prior_writer_live_until is not None:
            return ClaimUnitResult(
                ClaimUnitOutcome.PRIOR_WRITER_LIVE,
                request.redrive,
                held_until=self.prior_writer_live_until,
            )
        unit = self._unit()
        unit["status"] = "writing"
        unit["writer_execution_id"] = request.writer_execution_id
        if request.redrive:
            meta = self.world.plan_meta
            assert meta is not None
            self.world.plan_meta = replace(meta, failed_count=meta.failed_count - 1)
        self.world.unit_head = request.proof.resulting_head
        return ClaimUnitResult(ClaimUnitOutcome.CLAIMED, request.redrive)

    def acquire_table_fence(self, request: AcquireTableFenceRequest) -> AcquireTableFenceResult:
        self._record("acquire_table_fence", request)
        if self.acquire_outcome is not AcquireTableFenceOutcome.ACQUIRED:
            return AcquireTableFenceResult(self.acquire_outcome, held_until=self.held_until)
        self.world.lease = TableLeaseRecord(
            UNIT_KEY, request.holder, request.new_fencing_token, request.new_expires_at
        )
        return AcquireTableFenceResult(
            AcquireTableFenceOutcome.ACQUIRED,
            fencing_token=request.new_fencing_token,
            expires_at=request.new_expires_at,
        )

    def release_table_fence(self, request: ReleaseTableFenceRequest) -> ReleaseTableFenceResult:
        self._record("release_table_fence", request)
        return ReleaseTableFenceResult(ReleaseTableFenceOutcome.RELEASED)

    def begin_table_attempt(self, request: BeginTableAttemptRequest) -> BeginTableAttemptResult:
        self._record("begin_table_attempt", request)
        identity = request.identity()
        attempt_id = identity.attempt_id
        lease = request.lease
        unit = self._unit()
        unit.update(
            {
                "current_attempt_id": attempt_id,
                "ordinal": request.ordinal,
                "lease_holder": lease.holder,
                "fencing_token": lease.fencing_token,
                "reserved_slot_identity": request.reservation_identity,
                "unit_reservation_count": int(str(unit.get("unit_reservation_count", 0))) + 1,
            }
        )
        self.world.plan_reservation_count += 1
        self.world.attempts[attempt_id] = AttemptObservation(
            partition_key=f"TABLE_ATTEMPT#{UNIT_KEY}",
            record_key=f"ATTEMPT#{attempt_id}",
            state="intent",
            physical_table_id=UNIT_KEY,
            attempt_id=attempt_id,
            plan_unit_owner_ref=PLAN_KEY,
            inline_run_owner_ref=None,
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
            partition_key=f"TABLE_ATTEMPT#{UNIT_KEY}",
            record_key="FENCE",
            state="active",
            physical_table_id=UNIT_KEY,
            attempt_id=attempt_id,
            lease_holder=lease.holder,
            fencing_token=lease.fencing_token,
            parked_at=None,
            reconciliation=_pending(
                BindingDigestRequest(UNIT_KEY, attempt_id, lease.holder, lease.fencing_token)
            ),
        )
        return BeginTableAttemptResult(
            BeginTableAttemptOutcome.BEGUN,
            attempt_id,
            identity.client_request_token,
            identity.envelope_digest,
            request.ordinal,
        )

    def record_stage1_refusal(self, request: RecordStage1RefusalRequest) -> None:
        self._record("record_stage1_refusal", request)
        self.world.unit_head = request.proof.resulting_head

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
        unit = self._unit()
        references = cast("list[object]", unit.get("attempt_references", []))
        unit["attempt_references"] = [*references, attempt_id]
        unit["ordinal"] = int(str(unit["ordinal"])) + 1
        del unit["current_attempt_id"]
        return CompleteTableAttemptResult(CompleteTableAttemptOutcome.COMPLETED)

    def fail_unit(self, request: FailUnitRequest) -> FailUnitResult:
        self._record("fail_unit", request)
        if request.attempt_budget_exhausted:
            return FailUnitResult(FailUnitOutcome.FAILED, request.successor_intent_id())
        unit = self._unit()
        unit["status"] = "failed"
        return FailUnitResult(FailUnitOutcome.FAILED)

    def finalize_unit(self, request: FinalizeUnitRequest) -> FinalizeUnitResult:
        self._record("finalize_unit", request)
        if self.finalize_conflicts:
            self.finalize_conflicts -= 1
            raise DomainError(ErrorCode("PROJECTION_COORDINATOR", 1), "fixture pointer conflict")
        unit = self._unit()
        unit["status"] = "attested"
        meta = self.world.plan_meta
        assert meta is not None
        settled = tuple(
            sorted(
                (
                    *request.prior_settled_attempts,
                    *(
                        SettledAttemptReference(UNIT_KEY, str(m.attempt.attempt_id), USAGE_DIGEST)
                        for m in request.settlement.attempts
                    ),
                ),
                key=lambda ref: (ref.physical_table_id.encode(), ref.attempt_id.encode()),
            )
        )
        self.world.plan_meta = replace(
            meta,
            attested_count=meta.attested_count + 1,
            settled_attempt_count=len(settled),
            settled_attempt_digest=plan_settled_attempt_digest(settled) if settled else None,
        )
        for member in request.settlement.attempts:
            attempt_id = str(member.attempt.attempt_id)
            self.world.attempts[attempt_id] = replace(
                self.world.attempts[attempt_id], athena_usage_state="settled"
            )
        return FinalizeUnitResult(FinalizeUnitOutcome.FINALIZED)

    def complete_plan(self, request: CompletePlanRequest) -> CompletePlanResult:
        self._record("complete_plan", request)
        return CompletePlanResult(CompletePlanOutcome.COMPLETED)


@dataclass
class _Attempts:
    world: _World
    calls: list[str]
    requests: list[RecordSubmittedAttemptRequest] = field(
        default_factory=list[RecordSubmittedAttemptRequest]
    )

    def record_submitted_attempt(
        self, request: RecordSubmittedAttemptRequest
    ) -> RecordSubmittedAttemptResult:
        self.calls.append("record_submitted_attempt")
        self.requests.append(request)
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
    requests: list[StartTableAttemptRequest] = field(default_factory=list[StartTableAttemptRequest])

    def start_table_attempt(self, request: StartTableAttemptRequest) -> StartTableAttemptResult:
        self.calls.append("start")
        self.requests.append(request)
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
class _Terminals:
    """Scripted provider observations; ``nonterminal_until_stop`` models a long-running query."""

    calls: list[str]
    clock: _Clock
    terminal: ProviderTerminalObservation = field(default_factory=_terminal)
    readback: tuple[tuple[str | None, ...], ...] = ()
    nonterminal_until_stop: bool = False
    nonterminal_observations: int = 0
    stopped: list[str] = field(default_factory=list[str])
    observed: list[str] = field(default_factory=list[str])
    read_back: list[str] = field(default_factory=list[str])

    def observe(self, query_execution_id: str) -> ObserveTableAttemptResult:
        self.calls.append("observe")
        self.observed.append(query_execution_id)
        if self.nonterminal_until_stop and query_execution_id not in self.stopped:
            return ProviderNonterminal(reason="nonterminal")
        if self.observed.count(query_execution_id) <= self.nonterminal_observations:
            return ProviderNonterminal(reason="nonterminal")
        return self.terminal

    def stop(self, query_execution_id: str) -> None:
        self.calls.append("stop")
        self.stopped.append(query_execution_id)
        self.terminal = _terminal(ProviderTerminalState.CANCELLED)

    def readback_rows(self, query_execution_id: str) -> tuple[tuple[str | None, ...], ...]:
        self.calls.append("readback")
        self.read_back.append(query_execution_id)
        return self.readback


@dataclass
class _Objects:
    calls: list[str]
    body: bytes = OBJECT_BYTES

    def read(self, key: str) -> bytes:
        assert key == OBJECT_REFERENCE
        self.calls.append("read_object")
        return self.body


@dataclass
class _Visibility:
    calls: list[str]
    changes: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    failure: Exception | None = None

    def change(self, receipt_handle: str, timeout_seconds: int) -> None:
        self.calls.append("change_visibility")
        self.changes.append((receipt_handle, timeout_seconds))
        if self.failure is not None:
            raise self.failure


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
class _Harness:
    writer: Any
    world: _World
    coordinator: _Coordinator
    attempts: _Attempts
    start: _Start
    terminals: _Terminals
    objects: _Objects
    visibility: _Visibility
    clock: _Clock
    calls: list[str]
    runtime: Any
    stager: RecordingStager

    def expected_readback(self) -> tuple[tuple[str | None, ...], ...]:
        """ADR-149: the exact verification count rows for the unit's staged rows."""
        shaped = self.writer._shape_unit(self.runtime, UNIT)
        return workload_verification_rows(shaped.workload)

    def process(self, receive_count: int = 1) -> Any:
        delivery = self.writer.write_unit_delivery(_event(receive_count=receive_count))
        return self.writer._process_unit(self.runtime, delivery, self.clock.remaining_budget_ms)


@dataclass(frozen=True, slots=True)
class _HarnessOptions:
    """Scripted stub behaviour for one run; defaults are the clean two-statement path."""

    starts: tuple[StartTableAttemptResult, ...] = (_accepted("q-1"), _accepted("q-2"))
    acquire: AcquireTableFenceOutcome = AcquireTableFenceOutcome.ACQUIRED
    held_until: str | None = None
    prior_writer_live_until: str | None = None


def _harness(
    writer: Any, world: _World | None = None, options: _HarnessOptions | None = None
) -> _Harness:
    calls: list[str] = []
    selected = world or _World()
    chosen = options or _HarnessOptions()
    clock = _Clock()
    coordinator = _Coordinator(
        selected,
        calls,
        acquire_outcome=chosen.acquire,
        held_until=chosen.held_until,
        prior_writer_live_until=chosen.prior_writer_live_until,
    )
    attempts = _Attempts(selected, calls)
    start = _Start(calls, list(chosen.starts))
    terminals = _Terminals(calls, clock)
    objects = _Objects(calls)
    visibility = _Visibility(calls)
    stager = RecordingStager()
    runtime = writer._Runtime(
        reader=_Reader(selected),
        coordinator=cast("Any", coordinator),
        attempts=cast("Any", attempts),
        start=cast("Any", start),
        terminals=cast("Any", terminals),
        objects=cast("Any", objects),
        stager=stager,
        catalog=CATALOG,
        target=AthenaSemanticTarget(
            batch_workgroup="fictional-batch",
            source_database="fictional-source",
            candidate_database="fictional-candidate",
            current_database=CURRENT_DATABASE,
            staging_database="fictional_staging",
        ),
        visibility=cast("Any", visibility),
        wall=lambda: NOW,
        monotonic_ms=clock.monotonic_ms,
        sleep_ms=clock.sleep_ms,
        graph=cast("Any", GraphModeStub()),
        graph_facts=cast("Any", object()),
    )
    harness = _Harness(
        writer,
        selected,
        coordinator,
        attempts,
        start,
        terminals,
        objects,
        visibility,
        clock,
        calls,
        runtime,
        stager,
    )
    terminals.readback = harness.expected_readback()
    calls.clear()  # the readback fixture shaped the unit once; the run under test starts clean
    return harness


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": "1",
        "plan_key": PLAN_KEY,
        "plan_digest": PLAN_DIGEST,
        "origin_intent_id": INTENT_ID,
        "generation": 0,
        "unit_key": UNIT_KEY,
    }
    body.update(overrides)
    return body


def _event(*, receive_count: int = 1, body: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "Records": [
            {
                "messageId": MESSAGE_ID,
                "receiptHandle": RECEIPT_HANDLE,
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
                "body": json.dumps(body if body is not None else _body()),
            }
        ]
    }


class _Context:
    function_name = "table-writer"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:table-writer"
    memory_limit_in_mb = 2048
    aws_request_id = "request-fictional"

    def get_remaining_time_in_millis(self) -> int:
        return FULL_BUDGET_MS


# --- §4: the six-field body and the writer receive identity ---------------------------------------


def test_writer_execution_id_is_the_pinned_canonical_digest_and_differs_per_receive(
    writer: Any,
) -> None:
    first = writer.writer_execution_id(MESSAGE_ID, 1)
    second = writer.writer_execution_id(MESSAGE_ID, 2)

    assert first == WRITER_EXEC_1
    assert second == WRITER_EXEC_2
    assert first == canonical_authority_digest(["stage1-writer-execution/v1", MESSAGE_ID, 1])
    assert len(first) == 64 and first == first.lower() and len(first.encode()) <= 128
    with pytest.raises(ValueError, match="positive receive count"):
        writer.writer_execution_id(MESSAGE_ID, 0)


def test_delivery_parses_exactly_the_six_owner_fields_from_one_record(writer: Any) -> None:
    delivery = writer.write_unit_delivery(_event(receive_count=2))

    assert delivery.plan_key == PLAN_KEY
    assert delivery.plan_digest == PLAN_DIGEST
    assert delivery.origin_intent_id == INTENT_ID
    assert delivery.generation == 0
    assert delivery.unit_key == UNIT_KEY
    assert delivery.message_id == MESSAGE_ID
    assert delivery.receipt_handle == RECEIPT_HANDLE
    assert delivery.receive_count == 2
    assert delivery.internal_delivery_execution_id == EXEC64
    assert delivery.writer_execution_id == WRITER_EXEC_2


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_body(flow_id="public-flow"), "exactly the six owner fields"),
        ({key: value for key, value in _body().items() if key != "generation"}, "six owner"),
        (_body(schema_version="2"), "schema_version 1"),
        (_body(generation="0"), "JSON integer"),
        (_body(plan_key=plan_partition_key("3" * 64, EXEC64)), "does not reproduce"),
    ],
)
def test_delivery_rejects_extra_missing_or_drifted_owner_fields(
    writer: Any, body: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        writer.write_unit_delivery(_event(body=body))


def test_delivery_requires_exactly_one_record(writer: Any) -> None:
    with pytest.raises(ValueError, match="exactly one record"):
        writer.write_unit_delivery({"Records": []})
    two = {"Records": [_event()["Records"][0], _event()["Records"][0]]}  # type: ignore[index]
    with pytest.raises(ValueError, match="exactly one record"):
        writer.write_unit_delivery(two)


# --- §2.2 happy path ------------------------------------------------------------------------------


def test_happy_path_claims_fences_begins_completes_finalizes_releases_and_completes_plan(
    writer: Any,
) -> None:
    harness = _harness(writer)

    result = harness.process()

    assert result.acknowledge is True
    assert result.outcome == "attested"
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "read_object",
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
        "finalize_unit",
        "release_table_fence",
        "complete_plan",
    ]
    begins = [r for r in harness.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    assert [begin.statement_kind for begin in begins] == ["merge", "verification"]
    assert [begin.ordinal for begin in begins] == [1, 2]
    assert all(begin.writer_execution_id == WRITER_EXEC_1 for begin in begins)
    starts = harness.start.requests
    assert [s.client_request_token for s in starts] == [b.identity().attempt_id for b in begins]
    completes = [
        r for r in harness.coordinator.requests if isinstance(r, CompleteTableAttemptRequest)
    ]
    assert all((c.query_count, c.scanned_bytes, c.engine_ms) == (1, 2048, 350) for c in completes)
    finalize = next(r for r in harness.coordinator.requests if isinstance(r, FinalizeUnitRequest))
    assert finalize.writer_execution_id == WRITER_EXEC_1
    assert finalize.settlement.owner_disposition is OwnerDisposition.SUCCESSFUL
    assert len(finalize.settlement.attempts) == 2
    assert finalize.verified_checksums == (CHECKSUM_HEX,)
    assert [p.kind for p in finalize.pointers] == [PointerOutcomeKind.ADVANCED]
    assert finalize.pointers[0].projected_fencing_token == FENCING_TOKEN
    release = next(
        r for r in harness.coordinator.requests if isinstance(r, ReleaseTableFenceRequest)
    )
    assert release.lease == finalize.lease
    complete = next(r for r in harness.coordinator.requests if isinstance(r, CompletePlanRequest))
    assert len(complete.units) == 1 and len(complete.attempts) == 2


def test_immutable_object_checksum_is_compared_on_digest_bytes_without_the_algorithm_prefix(
    writer: Any,
) -> None:
    """Publications render ``sha256:<hex>``; roster entries keep the hex, so both must agree."""
    assert CANONICAL.publication.content_checksum.startswith("sha256:")
    harness = _harness(writer)

    shaped = harness.writer._shape_unit(harness.runtime, UNIT)

    assert shaped.content_checksums == (CHECKSUM_HEX,)
    assert shaped.servable[0].fencing_token == FENCING_TOKEN


def test_newer_pointer_is_omitted_as_projection_superseded_and_unit_still_attests(
    writer: Any,
) -> None:
    world = _World()
    world.current[PARTITION_IDENTITY] = _current_item(
        projected_publication_id="publication-fictional-newer",
        projected_fencing_token=FENCING_TOKEN + 1,
    )
    harness = _harness(writer, world)

    result = harness.process()

    assert result.acknowledge is True
    finalize = next(r for r in harness.coordinator.requests if isinstance(r, FinalizeUnitRequest))
    assert [p.kind for p in finalize.pointers] == [PointerOutcomeKind.PROJECTION_SUPERSEDED]
    assert finalize.pointers[0].observed_current is None


# --- §4 classification --------------------------------------------------------------------------


def test_replaced_plan_acknowledges_without_claim_or_statement(writer: Any) -> None:
    world = _World(owner=_owner(execution_id="34" * 32, generation=1))
    harness = _harness(writer, world)

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=True, outcome="plan_replaced")
    assert harness.calls == []


def test_incomplete_admission_is_retryable_without_claim(writer: Any) -> None:
    world = _World(unit_item=_unit_item(admission="pending"))
    harness = _harness(writer, world)

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="admission_incomplete")
    # 2026-10-06: the unit is redelivered after the short admission delay on its own receipt
    # handle, not the queue's 960 s visibility (was: no call at all).
    assert harness.calls == ["change_visibility"]
    assert harness.visibility.changes == [(RECEIPT_HANDLE, writer.admission_retry_seconds(1))]
    assert harness.visibility.changes == [(RECEIPT_HANDLE, 30)]


def test_other_retryable_classifications_keep_the_queue_visibility(writer: Any) -> None:
    # Positive control for the admission delay: an unreadable owner is retryable too, and it
    # changes no visibility.
    world = _World(owner=None)
    harness = _harness(writer, world)
    result = harness.process()
    assert result == writer._UnitResult(acknowledge=False, outcome="owner_unreadable")
    assert harness.visibility.changes == []


def test_the_admission_delays_cover_the_measured_admission_latency(writer: Any) -> None:
    measured_max_seconds = 0.125  # 10 occurrences on 2026-10-06 (task evidence)
    receives = 3  # semanticWriteMaxReceiveCount
    waits = [writer.admission_retry_seconds(count) for count in range(1, receives)]
    assert waits == [30, 60]
    assert min(waits) >= max(30, 3 * measured_max_seconds)
    assert sum(waits) >= 10 * measured_max_seconds
    assert sum(waits) > 60  # past one missed reconciler tick
    # The delay never exceeds the queue's own visibility, so it only ever shortens the wait.
    assert writer.admission_retry_seconds(receives) < 960


def test_attested_unit_runs_only_the_plan_completion_predicate(writer: Any) -> None:
    world = _World(
        unit_item=_unit_item("attested", writer_execution_id=WRITER_EXEC_1, attempt_references=[]),
        plan_meta=_plan_meta(attested_count=1),
    )
    harness = _harness(writer, world)

    result = harness.process(receive_count=2)

    assert result.acknowledge is True
    assert result.outcome == "attested_replay_completed"
    assert harness.calls == ["complete_plan"]


def test_failed_unit_redrive_claims_with_the_one_time_decrement(writer: Any) -> None:
    world = _World(
        unit_item=_unit_item("failed", failure_reason="statement_retries_exhausted"),
        plan_meta=_plan_meta(failed_count=1),
    )
    harness = _harness(writer, world)

    result = harness.process(receive_count=2)

    assert result.acknowledge is True
    claim = next(r for r in harness.coordinator.requests if isinstance(r, ClaimUnitRequest))
    assert claim.redrive is True
    assert claim.writer_execution_id == WRITER_EXEC_2
    assert world.plan_meta is not None and world.plan_meta.failed_count == 0


# --- §8 writing→writing reclaim of a dead writer's unit (D85) -------------------------------------


def _dead_writer_harness(writer: Any) -> _Harness:
    """Writer execution 1 claims, is refused busy, and dies; its unit stays ``writing``."""
    harness = _harness(writer, options=_HarnessOptions(acquire=AcquireTableFenceOutcome.TABLE_BUSY))
    assert harness.process().outcome == "refused_table_busy"
    unit = harness.world.unit_item
    assert unit is not None and unit["status"] == "writing"
    assert unit["writer_execution_id"] == WRITER_EXEC_1 and "current_attempt_id" not in unit
    harness.coordinator.acquire_outcome = AcquireTableFenceOutcome.ACQUIRED
    harness.calls.clear()
    harness.coordinator.requests.clear()
    return harness


def test_redelivery_reclaims_a_dead_writers_unit_and_resumes(writer: Any) -> None:
    harness = _dead_writer_harness(writer)

    result = harness.process(receive_count=2)

    assert result.acknowledge is True and result.outcome == "attested"
    assert harness.calls[:2] == ["claim_unit", "acquire_table_fence"]
    claim = next(r for r in harness.coordinator.requests if isinstance(r, ClaimUnitRequest))
    assert (claim.reclaim, claim.redrive) == (True, False)
    assert claim.writer_execution_id == WRITER_EXEC_2
    assert claim.prior_writer_execution_id == WRITER_EXEC_1
    assert claim.prior_lease_holder == queued_lease_holder(PLAN_KEY, UNIT_KEY, WRITER_EXEC_1)
    assert not any(isinstance(r, RecordStage1RefusalRequest) for r in harness.coordinator.requests)
    unit = harness.world.unit_item
    assert unit is not None and unit["writer_execution_id"] == WRITER_EXEC_2


def test_live_prior_holder_defers_the_message_with_busy_visibility_and_no_event(
    writer: Any,
) -> None:
    harness = _dead_writer_harness(writer)
    harness.coordinator.prior_writer_live_until = (
        NOW.replace(microsecond=0) + timedelta(seconds=50)
    ).isoformat()

    result = harness.process(receive_count=2)

    assert result == writer._UnitResult(acknowledge=False, outcome="prior_writer_live")
    assert harness.calls == ["claim_unit", "change_visibility"]
    # remaining = ceil(50 s - 0.789012 s) = 50 s; visibility = min(990, max(1, 50 + 60)) seconds.
    assert harness.visibility.changes[-1] == (RECEIPT_HANDLE, 110)
    unit = harness.world.unit_item
    assert unit is not None and unit["writer_execution_id"] == WRITER_EXEC_1


def test_foreign_writing_unit_bound_to_an_attempt_defers_without_claim_or_event(
    writer: Any,
) -> None:
    prior_holder = queued_lease_holder(PLAN_KEY, UNIT_KEY, WRITER_EXEC_1)
    attempt_id = "a" * 64
    world = _World(
        unit_item=_unit_item(
            "writing", writer_execution_id=WRITER_EXEC_1, current_attempt_id=attempt_id
        ),
        fence=FenceObservation(
            partition_key=f"TABLE_ATTEMPT#{UNIT_KEY}",
            record_key="FENCE",
            state="active",
            physical_table_id=UNIT_KEY,
            attempt_id=attempt_id,
            lease_holder=prior_holder,
            fencing_token=3,
            parked_at=None,
            reconciliation=_pending(BindingDigestRequest(UNIT_KEY, attempt_id, prior_holder, 3)),
        ),
    )
    harness = _harness(writer, world)

    result = harness.process(receive_count=2)

    assert result == writer._UnitResult(acknowledge=False, outcome="foreign_attempt_pending")
    assert harness.calls == ["change_visibility"]
    # The reconcile due lies 930 s ahead: the TABLE_ATTEMPT_PENDING rule gives min(990, 930 + 60).
    assert harness.visibility.changes == [(RECEIPT_HANDLE, 990)]
    assert world.unit_item is not None and world.unit_item["writer_execution_id"] == WRITER_EXEC_1


# --- §5 refusals ---------------------------------------------------------------------------------


def test_table_busy_records_refusal_changes_visibility_once_and_stays_retryable(
    writer: Any,
) -> None:
    held_until = (NOW.replace(microsecond=0) + timedelta(seconds=50)).isoformat()
    harness = _harness(
        writer,
        options=_HarnessOptions(acquire=AcquireTableFenceOutcome.TABLE_BUSY, held_until=held_until),
    )

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="refused_table_busy")
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "record_stage1_refusal",
        "change_visibility",
    ]
    refusal = next(
        r for r in harness.coordinator.requests if isinstance(r, RecordStage1RefusalRequest)
    )
    assert refusal.tag is Stage1RefusalTag.UNIT
    assert refusal.reason is Stage1RefusalReason.TABLE_BUSY
    assert refusal.unit is not None and refusal.unit.writer_execution_id == WRITER_EXEC_1
    # remaining = ceil(50 s - 0.789012 s) = 50 s; visibility = min(990, max(1, 50 + 60)) seconds.
    assert harness.visibility.changes == [(RECEIPT_HANDLE, 110)]


def test_nonterminal_fence_refuses_table_attempt_pending_before_any_acquisition(
    writer: Any,
) -> None:
    other_holder = "f" * 64
    world = _World(
        fence=FenceObservation(
            partition_key=f"TABLE_ATTEMPT#{UNIT_KEY}",
            record_key="FENCE",
            state="active",
            physical_table_id=UNIT_KEY,
            attempt_id="a" * 64,
            lease_holder=other_holder,
            fencing_token=3,
            parked_at=None,
            reconciliation=_pending(BindingDigestRequest(UNIT_KEY, "a" * 64, other_holder, 3)),
        )
    )
    harness = _harness(writer, world)

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="refused_table_attempt_pending")
    assert harness.calls == ["claim_unit", "record_stage1_refusal", "change_visibility"]
    refusal = next(
        r for r in harness.coordinator.requests if isinstance(r, RecordStage1RefusalRequest)
    )
    assert refusal.reason is Stage1RefusalReason.TABLE_ATTEMPT_PENDING
    # The reconcile due lies 930 s ahead: min(990, 930 + 60) seconds.
    assert harness.visibility.changes == [(RECEIPT_HANDLE, 990)]


def test_visibility_failure_propagates_as_table_writer_3_after_the_refusal_is_recorded(
    writer: Any,
) -> None:
    harness = _harness(writer, options=_HarnessOptions(acquire=AcquireTableFenceOutcome.TABLE_BUSY))
    harness.visibility.failure = DependencyError(
        writer._VISIBILITY_UNAVAILABLE, "Semantic write-unit visibility could not be changed."
    )

    with pytest.raises(DependencyError) as caught:
        harness.process()

    assert caught.value.code == writer._VISIBILITY_UNAVAILABLE
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "record_stage1_refusal",
        "change_visibility",
    ]
    assert harness.world.unit_item is not None
    assert harness.world.unit_item["status"] == "writing"


def test_refusal_visibility_boundaries_follow_the_signed_microsecond_rule(writer: Any) -> None:
    now = NOW_MICROS
    second = 1_000_000
    assert writer.refusal_visibility_seconds(now - second, now) == 60
    assert writer.refusal_visibility_seconds(now, now) == 60
    assert writer.refusal_visibility_seconds(now + 1, now) == 61
    assert writer.refusal_visibility_seconds(now + 929 * second, now) == 989
    assert writer.refusal_visibility_seconds(now + 930 * second, now) == 990
    assert writer.refusal_visibility_seconds(now + 930 * second + 1, now) == 990
    assert writer.refusal_visibility_seconds(None, now) == 990


# --- §5 statement protocol ------------------------------------------------------------------------


def test_insufficient_budget_starts_no_attempt_and_stays_retryable(writer: Any) -> None:
    harness = _harness(writer)
    harness.clock.now_ms = FULL_BUDGET_MS - 120_000

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="insufficient_budget")
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "read_object",
        "release_table_fence",
    ]
    assert harness.world.attempts == {}


def test_ambiguous_acceptance_records_nothing_polls_nothing_and_parks_at_safety_deadline(
    writer: Any,
) -> None:
    ambiguous = AcceptanceAmbiguous(
        ProviderErrorClass.NETWORK_TIMEOUT,
        SanitizedStartEvidence(None, ProviderErrorClass.NETWORK_TIMEOUT),
    )
    harness = _harness(writer, options=_HarnessOptions(starts=(ambiguous,)))

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="attempt_parked")
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "read_object",
        "begin_table_attempt",
        "start",
        "park_attempt",
    ]
    assert harness.clock.now_ms == SAFETY_DEADLINE_MS
    assert harness.terminals.observed == []
    assert harness.terminals.stopped == []
    (attempt,) = harness.world.attempts.values()
    assert attempt.state == "parked"
    assert attempt.query_execution_id is None
    assert harness.world.fence is not None and harness.world.fence.state == "parked"


def test_cancellation_at_q_persists_once_stops_once_and_completes_the_cancelled_terminal(
    writer: Any,
) -> None:
    harness = _harness(writer)
    harness.terminals.nonterminal_until_stop = True

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="statement_retryable")
    assert harness.calls.count("request_attempt_cancellation") == 1
    assert harness.calls.count("stop") == 1
    assert harness.calls.count("begin_table_attempt") == 1
    assert "park_attempt" not in harness.calls
    assert "fail_unit" not in harness.calls
    assert harness.calls[-1] == "release_table_fence"
    cancellation_index = harness.calls.index("request_attempt_cancellation")
    assert harness.calls[cancellation_index + 1 : cancellation_index + 4] == [
        "stop",
        "observe",
        "complete_table_attempt",
    ]
    assert harness.clock.now_ms >= CANCELLATION_DEADLINE_MS
    assert harness.clock.now_ms < SAFETY_DEADLINE_MS
    assert all(duration <= POLL_MS for duration in harness.clock.slept)
    complete = next(
        r for r in harness.coordinator.requests if isinstance(r, CompleteTableAttemptRequest)
    )
    assert complete.provider_state is ProviderTerminalState.CANCELLED


def test_submitted_attempt_parks_at_safety_deadline_without_a_new_poll(writer: Any) -> None:
    harness = _harness(writer)
    harness.terminals.nonterminal_until_stop = True

    def never_stop(query_execution_id: str) -> None:
        assert query_execution_id == "q-1"
        harness.calls.append("stop")

    harness.terminals.stop = never_stop  # type: ignore[method-assign]

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="attempt_parked")
    assert harness.calls[-1] == "park_attempt"
    assert harness.clock.now_ms == SAFETY_DEADLINE_MS
    # The adaptive ramp polls, then one poll per capped cadence step strictly before S
    # (the pre-Q remainder truncates to land exactly on Q); none at or after S.
    ramp_total = sum(POLL_RAMP_MS)
    expected = (
        len(POLL_RAMP_MS)
        + -(-(CANCELLATION_DEADLINE_MS - ramp_total) // POLL_MS)
        + (SAFETY_DEADLINE_MS - CANCELLATION_DEADLINE_MS) // POLL_MS
    )
    assert len(harness.terminals.observed) == expected
    assert harness.calls.count("stop") == 1
    assert harness.calls.count("request_attempt_cancellation") == 1
    assert "release_table_fence" not in harness.calls
    assert harness.world.lease is not None and harness.world.lease.parked_at is not None


def test_a_fast_statement_waits_only_the_short_ramp_prefix(writer: Any) -> None:
    """The adaptive cadence exists so a 1-3 s engine result is seen in under a second.

    A fixed 5 s cap made waiting 60% of statement wall in the wide-window run.
    """
    harness = _harness(writer)
    harness.terminals.nonterminal_observations = 2

    result = harness.process()

    assert result.outcome == "attested"
    # Two nonterminal observations cost exactly 250 ms then 500 ms per statement.
    per_statement = [250, 500]
    statements = len(harness.start.requests)
    assert harness.clock.slept == per_statement * statements
    assert harness.clock.now_ms == sum(per_statement) * statements


def test_the_poll_cadence_doubles_per_statement_and_holds_at_the_cap(writer: Any) -> None:
    harness = _harness(writer)
    harness.terminals.nonterminal_observations = 8

    result = harness.process()

    assert result.outcome == "attested"
    statements = len(harness.start.requests)
    per_statement = list(POLL_RAMP_MS) + [POLL_MS] * (8 - len(POLL_RAMP_MS))
    assert harness.clock.slept == per_statement * statements
    # The ramp restarts for every statement: a new attempt never inherits a stale cap.
    assert all(duration <= POLL_MS for duration in harness.clock.slept)


def test_a_four_second_merge_is_observed_within_half_a_second_of_finishing(writer: Any) -> None:
    """The 5 s cap first saw a ~4.1 s p50 MERGE at 7.75 s; the 500 ms cap sees it by 4.25 s.

    Sixteen nonterminal observations cover ~7.75 s of engine time per statement; no sleep may
    exceed the 500 ms cap, so the terminal is observed at most 500 ms after it exists.
    """
    harness = _harness(writer)
    harness.terminals.nonterminal_observations = 16

    result = harness.process()

    assert result.outcome == "attested"
    assert max(harness.clock.slept) == 500
    statements = len(harness.start.requests)
    assert harness.clock.slept == ([250] + [500] * 15) * statements
    assert harness.clock.now_ms == 7_750 * statements


def test_failed_terminal_on_the_last_receive_records_the_closed_unit_failure(
    writer: Any,
) -> None:
    harness = _harness(writer)
    harness.terminals.terminal = _terminal(ProviderTerminalState.FAILED)

    result = harness.process(receive_count=MAX_RECEIVES)

    assert result == writer._UnitResult(acknowledge=False, outcome="unit_failed")
    assert harness.calls[-2:] == ["fail_unit", "release_table_fence"]
    failure = next(r for r in harness.coordinator.requests if isinstance(r, FailUnitRequest))
    assert failure.reason is UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED
    assert failure.settlement.owner_disposition is OwnerDisposition.RETRYABLE_FAILURE
    assert len(failure.settlement.attempts) == 1
    assert failure.unit_item is not None and failure.budget_exhaustion is None
    assert harness.world.unit_item is not None
    assert harness.world.unit_item["status"] == "failed"


def test_failed_terminal_with_receives_remaining_leaves_the_unit_writing(writer: Any) -> None:
    harness = _harness(writer)
    harness.terminals.terminal = _terminal(ProviderTerminalState.FAILED)

    result = harness.process(receive_count=MAX_RECEIVES - 1)

    assert result == writer._UnitResult(acknowledge=False, outcome="statement_retryable")
    assert "fail_unit" not in harness.calls
    assert harness.calls[-1] == "release_table_fence"


def test_verification_mismatch_on_last_receive_fails_the_unit_with_its_own_reason(
    writer: Any,
) -> None:
    harness = _harness(writer)
    harness.terminals.readback = (("substituted", None),)

    result = harness.process(receive_count=MAX_RECEIVES)

    assert result.outcome == "unit_failed"
    failure = next(r for r in harness.coordinator.requests if isinstance(r, FailUnitRequest))
    assert failure.reason is UnitFailureReason.VERIFICATION_MISMATCH


def test_checksum_mismatch_runs_no_statement_and_fails_only_the_unit(writer: Any) -> None:
    world = _World(unit_item=_unit_item())
    substituted = _unit_entry(checksum="d" * 64)
    assert world.unit_item is not None
    world.unit_item["objects"] = [entry.to_member() for entry in substituted.objects]
    harness = _harness(writer, world)

    result = harness.process()

    assert result == writer._UnitResult(acknowledge=False, outcome="unit_failed")
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "read_object",
        "fail_unit",
        "release_table_fence",
    ]
    failure = next(r for r in harness.coordinator.requests if isinstance(r, FailUnitRequest))
    assert failure.reason is UnitFailureReason.CHECKSUM_MISMATCH
    assert failure.settlement.attempts == ()


def test_plan_attempt_33_makes_no_athena_call_and_delegates_to_fail_unit_with_successor(
    writer: Any,
) -> None:
    world = _World(plan_reservation_count=PLAN_RESERVATION_CAP)
    harness = _harness(writer, world)

    result = harness.process()

    assert result == writer._UnitResult(
        acknowledge=True, outcome="attempt_budget_successor_created"
    )
    assert harness.calls == [
        "claim_unit",
        "acquire_table_fence",
        "read_object",
        "fail_unit",
        "release_table_fence",
    ]
    failure = next(r for r in harness.coordinator.requests if isinstance(r, FailUnitRequest))
    assert failure.reason is UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED
    assert failure.unit_item is None
    assert failure.budget_exhaustion is not None
    assert failure.budget_exhaustion.permanent_reservation_count == PLAN_RESERVATION_CAP
    assert failure.budget_exhaustion.rejected_ordinal == PLAN_RESERVATION_CAP + 1
    assert failure.budget_exhaustion.successor_units == (UNIT,)
    assert failure.settlement.owner_disposition is OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE
    assert harness.start.requests == []
    assert harness.world.attempts == {}


def test_finalization_conflict_rebuilds_from_strong_reread_without_failing_the_unit(
    writer: Any,
) -> None:
    harness = _harness(writer)
    harness.coordinator.finalize_conflicts = 1

    result = harness.process()

    assert result.acknowledge is True
    assert harness.calls.count("finalize_unit") == 2
    assert "fail_unit" not in harness.calls


# --- Acknowledgement through the Lambda entry point -----------------------------------------------


def test_handler_reports_a_retryable_unit_as_a_partial_batch_failure(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(writer, options=_HarnessOptions(acquire=AcquireTableFenceOutcome.TABLE_BUSY))
    monkeypatch.setattr(writer, "RUNTIME", harness.runtime)

    response = writer.handler(_event(), _Context())

    assert response == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}


def test_handler_acknowledges_only_after_finalization_release_and_completion(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(writer)
    monkeypatch.setattr(writer, "RUNTIME", harness.runtime)

    response = writer.handler(_event(), _Context())

    assert response == {"batchItemFailures": []}
    assert harness.calls[-3:] == ["finalize_unit", "release_table_fence", "complete_plan"]


def test_handler_raises_on_a_malformed_body_so_the_batch_of_one_stays_unacknowledged(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(writer)
    monkeypatch.setattr(writer, "RUNTIME", harness.runtime)

    with pytest.raises(ValueError, match="six owner fields"):
        writer.handler(_event(body=_body(extra="field")), _Context())

    assert harness.calls == []


# --- C1 telemetry: unit records carry the status flows; one closed receive count ----------------


def test_a_unit_record_carries_its_unit_and_plan_flow_ids_exactly(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[Any] = []

    def capture(log: Any, _logger: Any) -> None:
        logged.append(log)

    monkeypatch.setattr(writer, "report_operation", capture)
    harness = _harness(writer, _World(unit_item=_unit_item(admission="pending")))
    harness.process()
    delivery = writer.write_unit_delivery(_event())
    unit_flow = derive_unit_identity(writer._unit_identity_request(delivery)).flow_id
    plan_flow = derive_plan_identity(
        PlanIdentityRequest(delivery.plan_digest, delivery.internal_delivery_execution_id)
    ).flow_id
    [record] = logged
    assert record.details == {
        "internal_plan_key": delivery.plan_key,
        "flow_id": unit_flow,
        "parent_flow_id": plan_flow,
        "unit_key": delivery.unit_key,
        "writer_execution_id": delivery.writer_execution_id,
        "receive_count": 1,
    }
    # Positive control: the two flows are distinct, so a swapped or reused value is caught.
    assert unit_flow != plan_flow


def test_the_receive_count_has_only_closed_dimensions(writer: Any) -> None:
    assert writer.unit_receive_dimensions("admission_incomplete", 1) == {
        "Outcome": "admission_incomplete",
        "FinalReceive": "false",
    }
    assert writer.unit_receive_dimensions("attested_replay_completed", 3) == {
        "Outcome": "attested_replay",
        "FinalReceive": "true",
    }
    # Positive control: a value outside the vocabulary never becomes a dimension value.
    assert writer.unit_receive_dimensions("plan_digest-abc123", 2) == {
        "Outcome": "unclassified",
        "FinalReceive": "false",
    }


def test_every_outcome_the_writer_can_return_is_in_the_closed_vocabulary(writer: Any) -> None:
    import re  # noqa: PLC0415

    source = pathlib.Path(writer.__file__).read_text()
    literals = set(re.findall(r'_(?:RetryableUnit|Acknowledged)\("([a-z_]+)"\)', source))
    literals |= set(
        re.findall(r'_UnitResult\(acknowledge=(?:True|False), outcome="([a-z_]+)"\)', source)
    )
    literals |= {f"plan_{status}" for status in ("completed", "superseded", "admission_failed")}
    assert literals and literals <= writer.UNIT_RECEIVE_OUTCOMES
    assert "admission_incomplete" in literals  # the scan found the retryable family


def test_every_graph_publish_outcome_is_in_the_closed_vocabulary(writer: Any) -> None:
    import re  # noqa: PLC0415

    from adapters.inbound.ingestion_handlers import graph_publish  # noqa: PLC0415

    source = pathlib.Path(graph_publish.__file__).read_text()
    literals = set(re.findall(r'PublishResult\((?:True|False), "([a-z_]+)"', source))
    assert "frontier_conflict" in literals  # the scan found the rolling race outcome
    assert literals <= writer.UNIT_RECEIVE_OUTCOMES
    assert writer.unit_receive_dimensions("frontier_conflict", 1) == {
        "Outcome": "frontier_conflict",
        "FinalReceive": "false",
    }


def test_the_handler_counts_each_receive_once_with_its_outcome(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted: list[tuple[str, int]] = []

    def count(outcome: str, receive_count: int) -> None:
        counted.append((outcome, receive_count))

    monkeypatch.setattr(writer, "_count_receive", count)
    harness = _harness(writer, options=_HarnessOptions(acquire=AcquireTableFenceOutcome.TABLE_BUSY))
    monkeypatch.setattr(writer, "RUNTIME", harness.runtime)
    writer.handler(_event(), _Context())
    assert counted == [("refused_table_busy", 1)]
    counted.clear()
    with pytest.raises(ValueError, match="six owner fields"):
        writer.handler(_event(body=_body(extra="field")), _Context())
    assert counted == []  # nothing parsed, so nothing to count against a unit


# --- S3: the mode-by-envelope acceptance table (V-12, V-13(a), V-23, V-9) -------------------------

_GRAPH_BINDING = {"dispatch_id": "d" * 64, "run_id": "r" * 64, "node_id": "n" * 64}
GRAPH_CONFLICT_UNIT = "u" * 64
GRAPH_ROOT_FLOW = "f" * 64


def _unit_owner(entry: CanonicalUnitEntry = UNIT) -> dict[str, object]:
    """S6 §2.5: a `write_unit` node's exact owner body (as `graph_materialize.unit_spec`)."""
    return {
        "schema_version": "1",
        "node": "write_unit",
        "unit": entry.to_member(),
        "conflict_unit": GRAPH_CONFLICT_UNIT,
        "root_flow_id": GRAPH_ROOT_FLOW,
    }


def _batch_owner(**overrides: object) -> dict[str, object]:
    """Slice 7 (D31): a one-member `write_unit_batch` envelope, the only unit message shape."""
    body: dict[str, object] = {
        "schema_version": "1",
        "node": "write_unit_batch",
        "batch_id": "b" * 64,
        "lock_key": f"TABLE#{UNIT_KEY}",
        "members": [{"run_id": "r" * 64, "node_id": "n" * 64, "dispatch_id": "d" * 64}],
    }
    body.update(overrides)
    return body


def _publish_owner(**overrides: object) -> dict[str, object]:
    """S6 §2.5: the `publish` node's exact owner body (as `post_grant_body`)."""
    body: dict[str, object] = {
        "schema_version": "1",
        "post_grant": "publish",
        "grant_id": "g" * 64,
        "granted_run": "r" * 64,
        "conflict_unit": GRAPH_CONFLICT_UNIT,
        "served_revision": NOW.isoformat(),
        "root_flow_id": GRAPH_ROOT_FLOW,
    }
    body.update(overrides)
    return body


def _process(harness: _Harness, body: dict[str, object] | None = None) -> Any:
    delivery = harness.writer.table_writer_delivery(_event(body=body))
    if isinstance(delivery, harness.writer.GraphEnvelope):
        return harness.writer._process_graph_envelope(
            harness.runtime, delivery, harness.clock.remaining_budget_ms
        )
    return harness.writer._process_unit(
        harness.runtime, delivery, harness.clock.remaining_budget_ms
    )


def test_s6_a_graph_unit_envelope_parses_exactly_its_owner_fields(writer: Any) -> None:
    # Conversion 1 (D31): the pre-1b single-unit body is refused as any other shape ...
    with pytest.raises(writer.GraphEnvelopeShapeError) as refused:
        writer.table_writer_delivery(_event(body={**_unit_owner(), **_GRAPH_BINDING}))
    assert str(refused.value.code) == "ADLY-TABLE_WRITER-0004"
    # ... and a one-member batch parses as the one unit envelope kind.
    envelope = writer.table_writer_delivery(_event(body=_batch_owner()))
    assert isinstance(envelope, writer.GraphEnvelope)
    assert envelope.kind is writer.GraphEnvelopeKind.WRITE_UNIT_BATCH
    assert (envelope.batch_id, envelope.lock_key) == ("b" * 64, f"TABLE#{UNIT_KEY}")
    assert envelope.members == (writer.GraphBatchMember("r" * 64, "n" * 64, "d" * 64),)
    assert envelope.binding is None
    publish = writer.table_writer_delivery(_event(body={**_publish_owner(), **_GRAPH_BINDING}))
    assert publish.kind is writer.GraphEnvelopeKind.PUBLISH
    assert (publish.grant_id, publish.granted_run) == ("g" * 64, "r" * 64)
    # The six-field legacy form stays legacy-only.
    assert isinstance(writer.table_writer_delivery(_event()), writer.WriteUnitDelivery)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**_body(), **_GRAPH_BINDING}, id="six_legacy_fields_plus_binding"),
        pytest.param({**_body(), "dispatch_id": "d" * 64}, id="partial_binding"),
        pytest.param({**_batch_owner(), "extra": 1}, id="unit_extra_field"),
        pytest.param(_batch_owner(node="bake"), id="unit_other_node"),
        pytest.param(_batch_owner(members=[{"run_id": "r" * 64}]), id="unit_malformed"),
        pytest.param(_batch_owner(schema_version="2"), id="unit_schema_drift"),
        pytest.param(
            {**_publish_owner(post_grant="bake"), **_GRAPH_BINDING}, id="publish_other_node"
        ),
        pytest.param({**_publish_owner(), **_GRAPH_BINDING, "unit": []}, id="mixed_shapes"),
    ],
)
def test_s6_any_other_graph_shape_is_an_invariant(writer: Any, body: dict[str, object]) -> None:
    with pytest.raises(writer.GraphEnvelopeShapeError) as refused:
        writer.table_writer_delivery(_event(body=body))
    assert isinstance(refused.value, InvariantError)
    assert str(refused.value.code) == "ADLY-TABLE_WRITER-0004"
    assert refused.value.context["signal"] == "GRAPH_ENVELOPE_SHAPE_INVALID"
    assert cast("Any", refused.value).message_id == MESSAGE_ID


@pytest.mark.parametrize(
    "mode", [AuthorityMode.LEGACY, AuthorityMode.SHADOW, AuthorityMode.LEGACY_DRAINING]
)
def test_v12_a_graph_envelope_outside_graph_is_refused_with_zero_effect(
    writer: Any, mode: AuthorityMode
) -> None:
    harness = _harness(writer)
    harness.runtime.graph.mode = ModeState(mode, 1)
    result = _process(harness, _batch_owner())
    assert result.acknowledge is False
    assert result.outcome == "graph_dispatch_not_active"
    assert harness.calls == []
    assert harness.coordinator.requests == []


def test_v12_a_legacy_envelope_in_shadow_runs_the_legacy_protocol_with_no_mode_check(
    writer: Any,
) -> None:
    harness = _harness(writer)
    result = harness.process()
    assert result.outcome == "attested"
    assert harness.runtime.graph.reads == 1  # one strong mode read per batch
    finalize = next(r for r in harness.coordinator.requests if isinstance(r, FinalizeUnitRequest))
    assert finalize.mode_guard is None


def test_v13a_after_a_draining_read_legacy_finalize_carries_the_mode_check(writer: Any) -> None:
    harness = _harness(writer)
    harness.runtime.graph.mode = ModeState(AuthorityMode.LEGACY_DRAINING, 2)
    result = harness.process()
    assert result.outcome == "attested"
    finalize = next(r for r in harness.coordinator.requests if isinstance(r, FinalizeUnitRequest))
    assert finalize.mode_guard is not None
    assert finalize.mode_guard.kind.value == "finalize_not_graph"
    assert finalize.mode_guard.observed == {
        "pk": "MODE#ingestion",
        "sk": "MODE",
        "mode": "LEGACY_DRAINING",
        "revision": 1,
    }


def test_v12_a_legacy_envelope_in_graph_without_its_terminal_is_refused(writer: Any) -> None:
    harness = _harness(writer)
    harness.runtime.graph.mode = ModeState(AuthorityMode.GRAPH, 4)
    result = harness.process()
    assert result.acknowledge is False
    assert result.outcome == "legacy_envelope_after_cutover"
    assert harness.calls == []


def test_v23_an_exact_finalized_legacy_replay_in_graph_is_acknowledged_with_zero_effect(
    writer: Any,
) -> None:
    world = _World(
        unit_item=_unit_item("attested", writer_execution_id=WRITER_EXEC_1, attempt_references=[]),
        plan_meta=_plan_meta(attested_count=1),
    )
    harness = _harness(writer, world)
    harness.runtime.graph.mode = ModeState(AuthorityMode.GRAPH, 4)
    result = harness.process()
    assert result.acknowledge is True
    assert result.outcome == "legacy_replay_acknowledged"
    assert harness.calls == []


STAGE1_FLOW = "77777777-7777-4777-8777-777777777777"


@dataclass
class _InlineStub:
    """The shared inline run, scripted: records each request and returns one outcome (§2.5)."""

    outcome: str = "finalized"
    error: BaseException | None = None
    requests: list[Any] = field(default_factory=list[Any])
    fences: list[Any] = field(default_factory=list[Any])
    deadline_cancelled: bool = False

    def __call__(self, runtime: Any, request: Any) -> Any:
        from adapters.inbound.ingestion_handlers.inline_run import (  # noqa: PLC0415
            InlineRunResult,
        )
        from adapters.outbound.telemetry import tracing  # noqa: PLC0415

        del runtime
        self.requests.append(request)
        self.fences.extend(request.graph_claims())
        with tracing.span(tracing.TABLE_INLINE_RUN, stage="run"):
            if self.error is not None:
                raise self.error
        return InlineRunResult(
            outcome=self.outcome,
            run_id="inline-run-fictional",
            retryable=False,
            flow_id=STAGE1_FLOW,
            execution_id="88888888-8888-4888-8888-888888888888",
            predecessor_execution_id=None,
            deadline_cancelled=self.deadline_cancelled,
        )


@dataclass
class _GraphUnitWorld:
    harness: _Harness
    runtime: Any
    runs: tuple[str, ...]
    graph: Any
    inline: Any
    """An `_InlineStub` or a `_BatchInline`: the scripted shared inline run."""
    sent: str

    @property
    def run(self) -> str:
        return self.runs[0]

    @property
    def batch_id(self) -> str:
        return str(json.loads(self.sent)["batch_id"])

    def node(self, index: int = 0) -> Any:
        from execution_graph_dispatch_support import node  # noqa: PLC0415

        return node(self.graph, self.runs[index], f"write_unit:{UNIT_KEY}")

    def nodes(self) -> list[Any]:
        return [self.node(index) for index in range(len(self.runs))]

    def run_record(self, index: int = 0) -> Any:
        from execution_graph_dispatch_support import store  # noqa: PLC0415

        return store(self.graph).read_run(self.runs[index])

    def lock(self) -> dict[str, Any]:
        return self.graph.items[(f"LOCK#TABLE#{UNIT_KEY}", "LOCK")]

    def open_members(self) -> int:
        return int(self.lock()["open_members"]["N"])

    def event(self, receive_count: int = 1) -> dict[str, object]:
        return {
            "Records": [
                {
                    "messageId": MESSAGE_ID,
                    "receiptHandle": RECEIPT_HANDLE,
                    "attributes": {"ApproximateReceiveCount": str(receive_count)},
                    "body": self.sent,
                }
            ]
        }

    def process(self, receive_count: int = 1) -> Any:
        writer = self.harness.writer
        envelope = writer.table_writer_delivery(self.event(receive_count))
        assert isinstance(envelope, writer.GraphEnvelope)
        return writer._process_graph_envelope(
            self.runtime, envelope, self.harness.clock.remaining_budget_ms
        )


def _unit_entry_for(index: int) -> CanonicalUnitEntry:
    """Slice 7: one more fictional publication on the same table, for a multi-member batch."""
    if index == 0:
        return UNIT
    (only,) = UNIT.objects
    return replace(
        UNIT, objects=(replace(only, publication_id=f"publication-fictional-{index + 1:04d}"),)
    )


def _graph_batch_world(  # noqa: PLR0913 - one fixture builder with explicit knobs
    writer: Any,
    *,
    entries: tuple[CanonicalUnitEntry, ...] = (UNIT,),
    fence: bool = False,
    inline: Any = None,
) -> _GraphUnitWorld:
    """Materialized runs -> arm pass -> dispatcher -> SQS bytes, over the real graph store.

    Slice 7 (conversion 1): each entry is one run's PENDING `write_unit` node on `TABLE#<k>`
    (the body `unit_spec` renders), armed as one batch by the promoter's arm pass and sent as
    one `write_unit_batch` message. Nothing is injected after parsing: the envelope TableWriter
    parses is the exact string the dispatcher sent.
    """
    from execution_graph_dispatch_support import (  # noqa: PLC0415
        TABLE,
        SpySender,
        finish,
        now_text,
        seed_run,
        set_mode,
        store,
    )
    from execution_graph_release_support import release_store  # noqa: PLC0415
    from execution_graph_support import FakeDynamo  # noqa: PLC0415

    from adapters.inbound.ingestion_handlers.graph_materialize import unit_spec  # noqa: PLC0415
    from adapters.jobs.execution_graph.dispatcher import (  # noqa: PLC0415
        DispatchContext,
        dispatch_shard,
    )
    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.instantiate import (  # noqa: PLC0415
        InstantiateRequest,
        ingestion_node_plan,
        instantiate_run,
    )
    from adapters.jobs.execution_graph.promoter import (  # noqa: PLC0415
        PromoterContext,
        arm_pass,
    )
    from domain.operations.execution_graph_dispatch import (  # noqa: PLC0415
        ArmScope,
        AttemptFact,
        FenceReason,
    )
    from ports.outbound.execution_graph_dispatch import (  # noqa: PLC0415
        FenceRunRequest,
        SuccessorRequest,
    )

    graph = FakeDynamo()
    set_mode(graph, AuthorityMode.GRAPH)
    runs: list[str] = []
    for index, entry in enumerate(entries):
        run = seed_run(
            graph,
            fingerprint=f"{index + 1:02x}" * 32,
            unit=canonical_authority_digest(["fictional-batch-window", str(index)]),
            request_id=f"request-batch-{index:04d}",
        ).record.run_id
        clock = (NOW - timedelta(hours=2, minutes=-index)).isoformat()
        graph.items[(f"RUN#{run}", "RUN")]["root_clock_started_at"] = {"S": clock}
        instantiate_run(store(graph), InstantiateRequest(run, ingestion_node_plan, now_text))
        finish(graph, run, "materialize_raw")
        record = store(graph).read_run(run)
        assert record is not None
        store(graph).put_successors(
            SuccessorRequest(record, (unit_spec(record, entry),), ArmScope.UNITS, True, now_text())
        )
        runs.append(run)
    arm_pass(
        PromoterContext(release_store(graph), ingestion_node_plan, lambda: NOW),
        due_shards(),
        lambda: 60_000,
        "test-request",
    )
    sender = SpySender()
    for shard in due_shards():
        dispatch_shard(DispatchContext(store(graph), sender, now_text), shard)
    batches = [call for call in sender.calls if '"write_unit_batch"' in call[0]]
    ((sent, group, _dedup),) = batches
    assert group == UNIT_KEY
    if fence:
        current = store(graph).read_run(runs[0])
        assert current is not None
        store(graph).fence_run(FenceRunRequest(current, FenceReason.CANCELLED, now_text()))

    class _GraphStore(type(store(graph))):  # type: ignore[misc]
        def read_mode_item(self) -> dict[str, object] | None:
            return None

    class _Facts:
        def attempt(self, *_args: object) -> tuple[Any, None]:
            return AttemptFact.NONE, None

    harness = _harness(writer)
    stub = inline or _InlineStub()
    runtime = replace(
        harness.runtime,
        graph=_GraphStore(TABLE, cast("Any", graph)),
        graph_facts=_Facts(),
        wall=lambda: NOW,
        inline_run=stub,
    )
    return _GraphUnitWorld(harness, runtime, tuple(runs), graph, stub, sent)


def _graph_unit_world(  # noqa: PLR0913 - one fixture builder with explicit knobs
    writer: Any,
    *,
    entry: CanonicalUnitEntry = UNIT,
    fence: bool = False,
    inline: _InlineStub | None = None,
) -> _GraphUnitWorld:
    """Build a batch of one: the built single-unit harness, generalized (conversion 1)."""
    return _graph_batch_world(writer, entries=(entry,), fence=fence, inline=inline)


def test_s3v2_v9_a_dispatched_envelope_for_a_fenced_run_is_acknowledged_superseded(
    writer: Any,
) -> None:
    world = _graph_unit_world(writer, fence=True)
    result = world.process()
    assert result.acknowledge is True
    assert result.outcome == "execution_superseded"
    assert world.harness.calls == []
    assert world.inline.requests == []
    assert world.node().state.value == "SUPERSEDED_UNCLAIMED"


def test_without_a_provisioned_graph_authority_every_batch_reads_legacy(writer: Any) -> None:
    harness = _harness(writer)
    harness.runtime = replace(harness.runtime, graph=None)
    result = harness.process()
    assert result.outcome == "attested"
    finalize = next(r for r in harness.coordinator.requests if isinstance(r, FinalizeUnitRequest))
    assert finalize.mode_guard is None
    graph_result = _process(harness, _batch_owner())
    assert graph_result.outcome == "graph_dispatch_not_active"


# --- S6: the drain counter and the `_fail_admission` residue --------------------------------------


def test_v17b_a_draining_batch_turns_drain_counting_on_before_any_transaction(
    writer: Any,
) -> None:
    harness = _harness(writer)
    stub = cast("GraphModeStub", harness.runtime.graph)
    stub.mode = ModeState(AuthorityMode.LEGACY_DRAINING, 2)
    harness.process()
    assert harness.coordinator.drain_reads == [AuthorityMode.LEGACY_DRAINING]


@pytest.mark.parametrize("mode", [AuthorityMode.LEGACY, AuthorityMode.SHADOW])
def test_v17b_positive_a_legacy_or_shadow_batch_counts_nothing(
    writer: Any, mode: AuthorityMode
) -> None:
    harness = _harness(writer)
    stub = cast("GraphModeStub", harness.runtime.graph)
    stub.mode = ModeState(mode, 1)
    harness.process()
    assert harness.coordinator.drain_reads == [mode]


def test_the_fail_admission_residue_is_acknowledged_with_zero_effect(writer: Any) -> None:
    """Acknowledge a unit sent before `_fail_admission` with zero effect.

    design.md, "Legacy cutover", step 5: the failed admission cleared the owner's active plan,
    so the delivery classifies `plan_replaced`: no claim, lease, statement, finalize or
    visibility change. It is effect-free and never reaches the DLQ.
    """
    owner = replace(_owner(), active_plan_key=None, active_intent_id=None, revision=3)
    harness = _harness(writer, _World(owner=owner))
    result = harness.process()
    assert result == writer._UnitResult(acknowledge=True, outcome="plan_replaced")
    assert harness.calls == []
    assert harness.coordinator.requests == []


# --- S6 §2.5: graph-unit execution and its outcome table (D5, D14) --------------------------------

_RETRY_OUTCOMES = (
    "refused_table_busy",
    "refused_table_attempt_pending",
    "insufficient_budget",
    "attempt_parked",
)
"""Conversion 6 (F1, D35): `statement_failed` on a lone member is a return (test 1b)."""


def _claim_item(world: _GraphUnitWorld) -> dict[str, Any]:
    return world.graph.items[(f"EXEC_CLAIM#{world.node().node_id}", "CLAIM")]


@pytest.fixture
def reported(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    from adapters.inbound.ingestion_handlers import graph_unit  # noqa: PLC0415

    errors: list[BaseException] = []

    def record(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(graph_unit, "report_error", record)
    return errors


def test_s6_d14_a_finalized_unit_settles_succeeded_with_its_effect_record(
    writer: Any, reported: list[BaseException]
) -> None:
    from adapters.inbound.ingestion_handlers.graph_unit import (  # noqa: PLC0415
        UNIT_EFFECT_NAMESPACE,
    )
    from domain.operations.table_attempt import WriterKind  # noqa: PLC0415

    world = _graph_unit_world(writer)
    result = world.process()
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_succeeded")
    node = world.node()
    assert node.state.value == "SUCCEEDED"
    record = json.loads(node.effect_record)
    assert record == {
        "schema_version": "1",
        "node": "write_unit",
        "unit_key": UNIT_KEY,
        "stage1_flow_id": STAGE1_FLOW,
        "servable_targets": [
            {
                "partition": PARTITION_IDENTITY,
                "publication_id": PUBLICATION_ID,
                "fencing_token": FENCING_TOKEN,
            }
        ],
    }
    assert node.effect_digest == canonical_authority_digest(
        [UNIT_EFFECT_NAMESPACE, node.effect_record]
    )
    # Conversion 4 (D33): the counted lock stays, free, under this batch.
    assert world.lock()["holder_batch_id"] == {"S": world.batch_id}
    assert world.open_members() == 0
    (request,) = world.inline.requests
    assert request.writer_kind is WriterKind.GRAPH_UNIT
    # Conversion 5 (D36): the inline run's IDs are the batch's and its execution set's.
    assert (request.operation_id, request.execution_id) == (
        world.batch_id,
        execution_set_id_for(world.batch_id, [node.node_id]),
    )
    assert (request.physical_table_id, request.servable) == (UNIT_KEY, ())
    (fence,) = world.inline.fences
    assert (fence.node_id, fence.lease_version) == (node.node_id, 1)
    assert fence.observed["lease_version"] == 1  # the exact strongly read claim item
    assert world.harness.coordinator.requests == []  # no legacy coordinator write
    assert reported == []


@pytest.mark.parametrize("outcome", _RETRY_OUTCOMES)
def test_s6_a_retryable_inline_outcome_with_receives_remaining_releases_the_lease(
    writer: Any, reported: list[BaseException], outcome: str
) -> None:
    world = _graph_unit_world(writer, inline=_InlineStub(outcome=outcome))
    result = world.process(receive_count=MAX_RECEIVES - 1)
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_retryable")
    assert world.node().state.value == "CLAIMED"
    assert _claim_item(world)["lease_expires_at"] == {"S": NOW.isoformat()}  # released (§2.3)
    assert world.run_record().state.value == "OPEN"
    assert world.harness.coordinator.requests == []
    assert reported == []


@pytest.mark.parametrize("outcome", _RETRY_OUTCOMES)
def test_s6_the_same_outcome_on_the_final_receive_settles_failed_and_fences(
    writer: Any, reported: list[BaseException], outcome: str
) -> None:
    world = _graph_unit_world(writer, inline=_InlineStub(outcome=outcome))
    result = world.process(receive_count=MAX_RECEIVES)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_failed")
    node = world.node()
    assert (node.state.value, node.failure_reason) == ("FAILED", outcome)
    run = world.run_record()
    assert (run.state.value, run.fence_reason.value) == ("FENCING", "node_failed")
    (error,) = reported
    assert str(error.code) == "ADLY-TABLE_WRITER-0006"  # type: ignore[attr-defined]
    assert error.context["signal"] == "GRAPH_RUN_FAILED"  # type: ignore[attr-defined]
    assert world.harness.coordinator.requests == []


@pytest.mark.parametrize("outcome", ["verification_mismatch", "attempt_budget_exhausted"])
def test_s6_a_mismatch_or_the_reservation_cap_is_final_on_the_first_receive(
    writer: Any, reported: list[BaseException], outcome: str
) -> None:
    world = _graph_unit_world(writer, inline=_InlineStub(outcome=outcome))
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_failed")
    assert (world.node().state.value, world.node().failure_reason) == ("FAILED", outcome)
    assert world.run_record().state.value == "FENCING"
    assert len(reported) == 1


def test_s6_a_checksum_mismatch_is_final_and_runs_no_statement(
    writer: Any, reported: list[BaseException]
) -> None:
    world = _graph_unit_world(writer, entry=_unit_entry(checksum="0" * 64))
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_failed")
    assert world.node().failure_reason == "checksum_mismatch"
    assert world.inline.requests == []
    assert len(reported) == 1


def test_s6_v9_a_takeover_refused_begin_acknowledges_with_zero_effect(
    writer: Any, reported: list[BaseException]
) -> None:
    """Conversion 8 (AR2, M1): a real takeover bumped the lease, so the member is lost."""
    from ports.outbound.projection_coordinator import (  # noqa: PLC0415
        GraphAuthorityRefusedError,
    )

    world = _graph_unit_world(writer)
    claimed: list[dict[Any, Any]] = []

    def refused(_runtime: Any, _request: Any) -> Any:
        item = _claim_item(world)
        item["lease_version"] = {"N": str(int(item["lease_version"]["N"]) + 1)}  # the takeover
        claimed.append(copy_items(world.graph))
        raise GraphAuthorityRefusedError("GRAPH_CLAIM_SUPERSEDED")

    world.runtime = replace(world.runtime, inline_run=refused)
    result = world.process()
    assert (result.acknowledge, result.outcome) == (True, "graph_claim_lost")
    assert copy_items(world.graph) == claimed[0]  # zero effect after the claim
    assert world.node().state.value == "CLAIMED"  # under the new holder
    assert reported == []


def test_slice7_m1_a_refused_begin_with_every_claim_live_drops_no_member(
    writer: Any, reported: list[BaseException]
) -> None:
    """Conversion 8's paired control (AR2, M1, N2): the built fixture, a live claim."""
    from adapters.inbound.ingestion_handlers.graph_claim import (  # noqa: PLC0415
        MAX_GUARD_REDECIDES,
    )
    from domain.shared_kernel.errors import GRAPH_UNIT_CLAIM_REFUSED_LIVE_CODE  # noqa: PLC0415
    from ports.outbound.projection_coordinator import (  # noqa: PLC0415
        GraphAuthorityRefusedError,
    )

    world = _graph_unit_world(writer)
    calls: list[int] = []

    def refused(_runtime: Any, _request: Any) -> Any:
        calls.append(1)
        raise GraphAuthorityRefusedError("GRAPH_CLAIM_SUPERSEDED")

    world.runtime = replace(world.runtime, inline_run=refused)
    errors: list[BaseException] = []
    original = world.harness.writer.report_error

    def capture(error: BaseException, report: object) -> None:
        errors.append(error)
        original(error, report)

    world.harness.writer.report_error = capture
    try:
        result = world.process()
    finally:
        world.harness.writer.report_error = original
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_claim_refused_live")
    assert len(calls) == 1 + MAX_GUARD_REDECIDES  # one run, then max_redecide re-decides
    assert world.node().state.value == "CLAIMED"
    assert _claim_item(world)["lease_expires_at"] == {"S": NOW.isoformat()}  # released
    assert _claim_item(world)["admission"] == {"N": "1"}  # no claim write (N2)
    (live,) = [e for e in errors if getattr(e, "code", None) == GRAPH_UNIT_CLAIM_REFUSED_LIVE_CODE]
    assert cast("Any", live).context["node_ids"] == [world.node().node_id]
    assert reported == []


@pytest.mark.parametrize(
    ("receive_count", "outcome", "state"),
    [(1, "graph_unit_retryable", "CLAIMED"), (MAX_RECEIVES, "graph_unit_failed", "FAILED")],
)
def test_s6_a_dependency_error_retries_until_the_final_receive(  # noqa: PLR0913, PLR0917
    writer: Any,
    reported: list[BaseException],
    receive_count: int,
    outcome: str,
    state: str,
) -> None:
    stub = _InlineStub(error=DependencyError(ErrorCode("FICTIONAL", 1), "injected"))
    world = _graph_unit_world(writer, inline=stub)
    result = world.process(receive_count=receive_count)
    assert result.outcome == outcome
    assert world.node().state.value == state
    if state == "FAILED":
        assert world.node().failure_reason == "dependency_exhausted"
    assert len(reported) == 1  # reported once per receive


def test_s6_a_final_failure_on_a_fencing_run_settles_with_no_fence_member(
    writer: Any, reported: list[BaseException]
) -> None:
    from domain.operations.execution_graph_dispatch import FenceReason  # noqa: PLC0415
    from ports.outbound.execution_graph_dispatch import FenceRunRequest  # noqa: PLC0415

    world = _graph_unit_world(writer, inline=_InlineStub(outcome="verification_mismatch"))

    def cancel(_runtime: Any, request: Any) -> Any:
        run = world.run_record()
        world.runtime.graph.fence_run(FenceRunRequest(run, FenceReason.CANCELLED, NOW.isoformat()))
        return _InlineStub(outcome="verification_mismatch")(_runtime, request)

    world.runtime = replace(world.runtime, inline_run=cancel)
    result = world.process()
    assert result.outcome == "graph_unit_failed"
    assert world.node().state.value == "FAILED"
    assert world.run_record().fence_reason.value == "cancelled"  # not re-fenced
    assert reported[0].context["fenced"] is False  # type: ignore[attr-defined]


def copy_items(graph: Any) -> dict[Any, Any]:
    """Snapshot the graph table for a zero-effect comparison."""
    return json.loads(json.dumps({"|".join(key): item for key, item in graph.items.items()}))


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Any:
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )

    from adapters.outbound.telemetry import tracing  # noqa: PLC0415

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    return exporter


@pytest.mark.usefixtures("reported")
def test_s6_each_graph_unit_opens_exactly_one_graph_native_root_span(
    writer: Any, exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _graph_unit_world(writer)
    monkeypatch.setattr(writer, "RUNTIME", world.runtime)
    assert writer.handler(world.event(), _Context()) == {"batchItemFailures": []}
    spans = exporter.get_finished_spans()
    (unit,) = [item for item in spans if item.name == "ingest.table_writer.graph_unit"]
    attributes = dict(unit.attributes or {})
    node = world.node()
    assert attributes["run_id"] == world.run
    assert attributes["node_id"] == node.node_id
    assert attributes["dispatch_id"] == node.dispatch_id
    batch_attributes = dict(
        next(i for i in spans if i.name.endswith("graph_unit_batch")).attributes or {}
    )
    assert batch_attributes["dispatch_id"] == batch_dispatch_id(world.batch_id, world.sent)
    assert batch_attributes["member_count"] == 1
    assert attributes["node"] == f"write_unit:{UNIT_KEY}"  # P8: the node name
    assert attributes["batch_id"] == world.batch_id
    assert attributes["outcome"] == "graph_unit_succeeded"
    (batch,) = [item for item in spans if item.name == "ingest.table_writer.graph_unit_batch"]
    # Conversion 2 (D37): the unit span's parent is the batch span, the envelope's root.
    assert batch.parent is None
    assert unit.parent is not None and unit.parent.span_id == batch.context.span_id
    assert not {"internal_plan_key", "writer_execution_id", "flow_id"} & set(attributes)
    assert [item.name for item in spans if item.name.startswith("ingest.table_writer")] == [
        "ingest.table_writer.graph_unit",
        "ingest.table_writer.graph_unit_batch",
    ]
    (inline,) = [item for item in spans if item.name == "ingest.table_inline_run"]
    assert inline.parent is not None and inline.parent.span_id == batch.context.span_id


def test_s6_control_a_legacy_envelope_opens_table_writer_process_and_no_graph_span(
    writer: Any, exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(writer)
    monkeypatch.setattr(writer, "RUNTIME", harness.runtime)
    assert writer.handler(_event(), _Context()) == {"batchItemFailures": []}
    names = [item.name for item in exporter.get_finished_spans()]
    assert names.count("ingest.table_writer") == 1
    assert "ingest.table_writer.graph_unit" not in names


def test_s6_a_malformed_graph_envelope_is_an_invariant_with_zero_effect(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _graph_unit_world(writer)
    errors: list[BaseException] = []
    counted: list[str] = []

    def record(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(writer, "report_error", record)

    def count(outcome: str, _receive_count: int) -> None:
        counted.append(outcome)

    monkeypatch.setattr(writer, "_count_receive", count)
    monkeypatch.setattr(writer, "RUNTIME", world.runtime)
    before = copy_items(world.graph)
    sent = json.loads(world.sent)
    malformed = {**sent, "extra": "field"}
    event = world.event()
    event["Records"][0]["body"] = json.dumps(malformed)  # type: ignore[index]
    assert writer.handler(event, _Context()) == {
        "batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]
    }
    (error,) = errors
    assert str(error.code) == "ADLY-TABLE_WRITER-0004"  # type: ignore[attr-defined]
    assert counted == ["graph_envelope_invalid"]
    assert copy_items(world.graph) == before
    assert world.inline.requests == []
    # Positive control: the exact unit shape proceeds to its claim and execution.
    assert writer.handler(world.event(), _Context()) == {"batchItemFailures": []}
    assert world.node().state.value == "SUCCEEDED"


def test_s6_the_new_graph_outcomes_are_in_the_closed_receive_vocabulary(writer: Any) -> None:
    for outcome in (
        "graph_unit_succeeded",
        "graph_unit_retryable",
        "graph_unit_failed",
        "graph_claim_lost",
        "graph_redecide_exhausted",
        "graph_envelope_invalid",
    ):
        assert outcome in writer.UNIT_RECEIVE_OUTCOMES


# --- S6 §2.6: the publish envelope's root span ----------------------------------------------------


def _publish_runtime(writer: Any) -> tuple[Any, Any]:
    from execution_graph_dispatch_support import TABLE, store  # noqa: PLC0415
    from graph_publish_support import (  # noqa: PLC0415
        NOW as PUBLISH_NOW,
    )
    from graph_publish_support import (  # noqa: PLC0415
        Tables,
        publication,
        publish_world,
        seed_current,
    )

    from adapters.inbound.ingestion_handlers.graph_publish import PublishPorts  # noqa: PLC0415

    fake = Tables()
    target = publication(date(2026, 9, 1), "a-new", 2, curation=True)
    seed_current(fake, target, None)
    world = publish_world(units={"db.table_a": (target,)}, fake=fake, claimed=False)

    class _GraphStore(type(store(fake))):  # type: ignore[misc]
        def read_mode_item(self) -> dict[str, object] | None:
            return None

    harness = _harness(writer)
    runtime = replace(
        harness.runtime,
        graph=_GraphStore(TABLE, cast("Any", fake)),
        wall=lambda: PUBLISH_NOW,
        publish=PublishPorts(world.publications, TABLE, lambda _signal: None),
    )
    return world, runtime


def test_s6_each_publish_opens_exactly_one_graph_publish_root_span_with_its_grant(
    writer: Any, exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, runtime = _publish_runtime(writer)
    monkeypatch.setattr(writer, "RUNTIME", runtime)
    node = world.publish_node()
    event = {
        "Records": [
            {
                "messageId": MESSAGE_ID,
                "receiptHandle": RECEIPT_HANDLE,
                "attributes": {"ApproximateReceiveCount": "1"},
                "body": node.body,
            }
        ]
    }
    assert writer.handler(event, _Context()) == {"batchItemFailures": []}
    spans = exporter.get_finished_spans()
    (publish,) = [item for item in spans if item.name == "ingest.graph_publish"]
    attributes = dict(publish.attributes or {})
    assert attributes["grant_id"] == world.envelope.grant_id
    assert (attributes["run_id"], attributes["node_id"]) == (world.run, node.node_id)
    assert attributes["dispatch_id"] == node.dispatch_id
    assert (attributes["advanced_count"], attributes["superseded_count"]) == (1, 0)
    assert attributes["outcome"] == "graph_publish_succeeded"
    assert publish.parent is None
    assert not [item for item in spans if item.name.startswith("ingest.table_writer")]
    assert world.publish_node().state.value == "SUCCEEDED"


def test_s6_the_publish_outcomes_are_in_the_closed_receive_vocabulary(writer: Any) -> None:
    for outcome in (
        "graph_publish_succeeded",
        "graph_publish_retryable",
        "graph_publish_invariant",
    ):
        assert outcome in writer.UNIT_RECEIVE_OUTCOMES


# --- Slice 7 (1b): batch execution (design §2.5 "Batch execution"; impl-plan slice 7 tests) ------


@dataclass
class _BatchInline:
    """A scripted shared inline run: fails any set holding a poison identity (test 4).

    `script` overrides the outcome of each call in order; every call evaluates the claim thunk
    once, as the statement protocol does for each Begin (V-9).
    """

    poison: frozenset[str] = frozenset()
    script: list[str] = field(default_factory=list[str])
    deadline_cancelled: bool = False
    requests: list[Any] = field(default_factory=list[Any])
    clock: Any = None
    elapsed_ms: int = 0

    def __call__(self, runtime: Any, request: Any) -> Any:
        from adapters.inbound.ingestion_handlers.inline_run import (  # noqa: PLC0415
            InlineRunResult,
        )

        del runtime
        self.requests.append(request)
        request.graph_claims()
        if self.clock is not None:
            self.clock.now_ms += self.elapsed_ms
        identities = {identity for identity, _ in request.workload.records_by_identity}
        if self.script:
            outcome = self.script.pop(0)
        elif identities & self.poison:
            outcome = "statement_failed"
        else:
            outcome = "finalized"
        return InlineRunResult(
            outcome=outcome,
            run_id="inline-run-fictional",
            retryable=False,
            flow_id=STAGE1_FLOW,
            execution_id="88888888-8888-4888-8888-888888888888",
            predecessor_execution_id=None,
            deadline_cancelled=self.deadline_cancelled,
        )

    def sets(self) -> list[set[str]]:
        return [
            {identity for identity, _ in request.workload.records_by_identity}
            for request in self.requests
        ]


def _publication(index: int) -> str:
    return _unit_entry_for(index).objects[0].publication_id


def _batch_shaper(
    writer: Any, monkeypatch: pytest.MonkeyPatch, *, rows: dict[int, int] | None = None
) -> None:
    """Shape every entry from the one fixture object, keyed by its own publication identity.

    The fake object store holds one canonical object; a multi-member batch needs one identity
    per member, so each shaped workload carries the fixture's records under its entry's
    publication ID (`rows` repeats a member's record to give it more merges).
    """
    real = writer._shape_unit
    base = real(_harness(writer).runtime, UNIT)
    ((_, records),) = base.workload.records_by_identity
    table = base.workload.table
    partition = table.partition_keys[0]

    def varied(record: dict[str, Any], n: int) -> dict[str, Any]:
        out = dict(record)
        for name in table.grain:
            value = out[name]
            if name == partition or n == 0:
                continue
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, int):
                out[name] = value + n
            elif isinstance(value, date):
                out[name] = value + timedelta(days=n)
            else:
                out[name] = f"{value}-{n}"
        return out

    def shape(runtime: Any, entry: CanonicalUnitEntry) -> Any:
        del runtime
        (item,) = entry.objects
        index = int(item.publication_id.rsplit("-", 1)[1]) - 1
        count = (rows or {}).get(index, 1)
        own = tuple({**varied(records[0], n), partition: item.publication_id} for n in range(count))
        (servable,) = base.servable
        return replace(
            base,
            workload=replace(base.workload, records_by_identity=((item.publication_id, own),)),
            servable=(replace(servable, publication_id=item.publication_id),),
        )

    monkeypatch.setattr(writer, "_shape_unit", shape)


def _batch_world(  # noqa: PLR0913, PLR0917 - one fixture builder
    writer: Any, monkeypatch: pytest.MonkeyPatch, count: int, inline: Any = None
) -> _GraphUnitWorld:
    _batch_shaper(writer, monkeypatch)
    return _graph_batch_world(
        writer,
        entries=tuple(_unit_entry_for(index) for index in range(count)),
        inline=inline or _BatchInline(),
    )


def _states(world: _GraphUnitWorld) -> list[str]:
    return [node.state.value for node in world.nodes()]


def test_slice7_1_two_members_that_fit_together_compose_one_shorter_set(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    world = _batch_world(writer, monkeypatch, 2)
    result = world.process()
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_batch_settled")
    (request,) = world.inline.requests
    assert len(request.composition.statements) == 2  # one MERGE and one verification
    assert request.execution_id == execution_set_id_for(
        world.batch_id, [node.node_id for node in world.nodes()]
    )
    assert len(request.graph_claims()) == 2  # one fence per member (D36)
    assert _states(world) == ["SUCCEEDED", "SUCCEEDED"]
    assert world.open_members() == 0
    assert reported == []
    # Control: each member alone composes two statements, four in all.
    assert sum(len(item.statements) for item in _alone_compositions(writer)) == 4


def _alone_compositions(writer: Any) -> list[Any]:
    from adapters.outbound.athena.table_attempt_statements import (  # noqa: PLC0415
        ComposeUnitStatementsRequest,
        compose_unit_statements,
    )

    shaped = [writer._shape_unit(None, _unit_entry_for(index)) for index in range(2)]
    return [
        compose_unit_statements(
            ComposeUnitStatementsRequest(
                workload=item.workload,
                workgroup="fictional-batch",
                staging_database="fictional_staging",
            )
        )
        for item in shaped
    ]


def test_slice7_1b_a_lone_statement_failure_with_attempts_left_is_a_return(
    writer: Any, reported: list[BaseException]
) -> None:
    world = _graph_unit_world(writer, inline=_InlineStub(outcome="statement_failed"))
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_returned")
    node = world.node()
    assert (node.state.value, node.return_reason, node.arm_alone) == (
        "PENDING",
        "member_failed",
        True,
    )
    assert node.batch_id is None
    assert world.open_members() == 0
    assert _claim_item(world)["lease_expires_at"] == {"S": NOW.isoformat()}
    arms = [key for key in world.graph.items if key[1] == f"ARM#{world.run}"]
    assert len(arms) == 1  # re-put for the next arm pass
    assert reported == []
    # Control: on the final attempt the same outcome settles FAILED, fenced, reported once.
    final = _graph_unit_world(writer, inline=_InlineStub(outcome="statement_failed"))
    settled = final.process(receive_count=MAX_RECEIVES)
    assert (settled.acknowledge, settled.outcome) == (True, "graph_unit_failed")
    assert (final.node().state.value, final.node().failure_reason) == ("FAILED", "statement_failed")
    assert final.run_record().fence_reason.value == "node_failed"
    (error,) = reported
    assert str(cast("Any", error).code) == "ADLY-TABLE_WRITER-0006"
    assert cast("Any", error).context["batch_id"] == final.batch_id


def test_slice7_1b_a_shaping_dependency_error_returns_and_a_run_one_is_unresolved(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    real = writer._shape_unit
    failing = [True]

    def shape(runtime: Any, entry: Any) -> Any:
        if failing[0]:
            raise DependencyError(ErrorCode("FICTIONAL", 2), "injected object read")
        return real(runtime, entry)

    world = _graph_unit_world(writer)
    monkeypatch.setattr(writer, "_shape_unit", shape)
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_returned")
    assert world.node().state.value == "PENDING"
    assert world.inline.requests == []
    assert reported == []  # a return is not a failure: no alarm
    failing[0] = False
    # Control: the same error raised by the run leaves the member unresolved, lock held.
    stub = _InlineStub(error=DependencyError(ErrorCode("FICTIONAL", 1), "injected"))
    control = _graph_unit_world(writer, inline=stub)
    unresolved = control.process(receive_count=1)
    assert (unresolved.acknowledge, unresolved.outcome) == (False, "graph_unit_retryable")
    assert control.node().state.value == "CLAIMED"
    assert control.open_members() == 1
    assert len(reported) == 1


@pytest.mark.parametrize("outcome", ["refused_table_busy", "statement_failed"])
def test_slice7_1c_a_member_at_admission_3_on_a_first_receive_takes_its_final_row(
    writer: Any, reported: list[BaseException], outcome: str
) -> None:
    del reported
    world = _graph_unit_world(writer, inline=_InlineStub(outcome="refused_table_busy"))
    for admission in (1, 2):  # two unresolved copies of the batch, each at receive 1
        assert world.process(receive_count=1).outcome == "graph_unit_retryable"
        assert _claim_item(world)["admission"] == {"N": str(admission)}
    # Control: at admission 2 on receive 1 the member still had attempts left (above).
    world.inline.outcome = outcome
    result = world.process(receive_count=1)
    assert _claim_item(world)["admission"] == {"N": "3"}
    assert (result.outcome, world.node().state.value) == ("graph_unit_failed", "FAILED")
    assert world.node().failure_reason == outcome


def _remaining(world: _GraphUnitWorld, remaining_ms: int) -> None:
    world.harness.clock.now_ms = FULL_BUDGET_MS - remaining_ms


def test_slice7_1d_a_batch_of_one_runs_no_time_rule_and_a_batch_of_two_does(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain.operations.statement_protocol import MIN_EXCLUSIVE_BUDGET_MS  # noqa: PLC0415

    alone = _graph_unit_world(writer)
    _remaining(alone, MIN_EXCLUSIVE_BUDGET_MS + 1)
    assert alone.process().outcome == "graph_unit_succeeded"
    assert len(alone.inline.requests) == 1  # it began, below the rule's threshold
    # Control: the same budget with two claimed members makes no Begin; both unresolved.
    pair = _batch_world(writer, monkeypatch, 2)
    _remaining(pair, MIN_EXCLUSIVE_BUDGET_MS + 1)
    result = pair.process()
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert pair.inline.requests == []
    assert _states(pair) == ["CLAIMED", "CLAIMED"]
    assert pair.open_members() == 2


@pytest.mark.parametrize(
    ("script", "deadline"),
    [(["insufficient_budget"], False), (["attempt_parked"], False), (["statement_failed"], True)],
)
def test_slice7_1d_time_outcomes_in_a_batch_of_three_are_budget_outcomes(  # noqa: PLR0913, PLR0917
    writer: Any,
    monkeypatch: pytest.MonkeyPatch,
    reported: list[BaseException],
    script: list[str],
    deadline: bool,
) -> None:
    world = _batch_world(
        writer, monkeypatch, 3, _BatchInline(script=list(script), deadline_cancelled=deadline)
    )
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert len(world.inline.requests) == 1  # no split runs
    assert _states(world) == ["CLAIMED"] * 3
    # On the final attempt every member returns final_unisolated; none fails.
    world.inline.script = list(script)
    final = world.process(receive_count=MAX_RECEIVES)
    assert final.acknowledge is True
    nodes = world.nodes()
    assert {(n.state.value, n.return_reason, n.final_return) for n in nodes} == {
        ("PENDING", "final_unisolated", True)
    }
    assert reported == []


def test_slice7_1d_control_a_plain_statement_failure_on_three_members_splits(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _batch_world(writer, monkeypatch, 3, _BatchInline(script=["statement_failed"]))
    result = world.process(receive_count=1)
    assert result.acknowledge is True
    assert len(world.inline.requests) == 3  # the set, then its two halves
    assert _states(world) == ["SUCCEEDED"] * 3


def test_slice7_n4_a_pending_attempt_fails_members_only_on_the_final_attempt(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    """N4's carve-out: a table-level outcome on the final attempt fails each member."""
    world = _batch_world(
        writer, monkeypatch, 3, _BatchInline(script=["refused_table_attempt_pending"])
    )
    assert world.process(receive_count=2).outcome == "graph_unit_batch_unresolved"
    assert _states(world) == ["CLAIMED"] * 3  # control: attempts remain, unresolved
    world.inline.script = ["refused_table_attempt_pending"]
    world.process(receive_count=MAX_RECEIVES)
    assert {(n.state.value, n.failure_reason) for n in world.nodes()} == {
        ("FAILED", "refused_table_attempt_pending")
    }
    assert len(reported) == 3


def test_slice7_3_a_shared_identity_starts_the_next_set_and_distinct_ones_share(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _batch_shaper(writer, monkeypatch)
    entries = (_unit_entry_for(0), _unit_entry_for(1), _unit_entry_for(1))
    world = _graph_batch_world(writer, entries=entries, inline=_BatchInline())
    result = world.process()
    assert result.acknowledge is True
    assert world.inline.sets() == [{_publication(0), _publication(1)}, {_publication(1)}]
    # Control: distinct identities share one set (test 1 above), with no InvariantError.
    assert _states(world) == ["SUCCEEDED"] * 3


def test_slice7_3_a_union_composition_failure_is_an_invariant_with_zero_effect(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain.shared_kernel.errors import GRAPH_UNIT_SET_COMPOSITION_CODE  # noqa: PLC0415

    real = writer.compose_unit_statements

    def faulty(request: Any) -> Any:
        if len(request.workload.records_by_identity) > 1:
            raise ValueError("injected union defect")
        return real(request)

    world = _batch_world(writer, monkeypatch, 2)
    monkeypatch.setattr(writer, "compose_unit_statements", faulty)
    with pytest.raises(InvariantError) as refused:
        world.process()
    assert refused.value.code == GRAPH_UNIT_SET_COMPOSITION_CODE
    assert world.inline.requests == []
    assert _states(world) == ["CLAIMED", "CLAIMED"]
    assert _claim_item(world)["lease_expires_at"] == {"S": NOW.isoformat()}  # D-b
    # Control: the same composer on one-member workloads composes, and the batch settles.
    alone = _graph_unit_world(writer)
    assert alone.process().outcome == "graph_unit_succeeded"


def test_slice7_ar5_3_a_member_alone_over_ten_statements_is_its_own_failure(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    """ADR-149: a member alone always composes two statements, so none is `member_failed`."""
    from adapters.outbound.athena.start_envelope import (  # noqa: PLC0415
        ComposeStartEnvelopeRequest,
        compose_start_envelope,
    )
    from adapters.outbound.athena.table_attempt_statements import (  # noqa: PLC0415
        compose_unit_statements,
    )

    _batch_shaper(writer, monkeypatch, rows={1: 10, 2: 9})
    entries = tuple(_unit_entry_for(index) for index in range(3))
    for entry in entries:
        workload = writer._shape_unit(None, entry).workload
        alone = compose_unit_statements(writer._compose(workload, _harness(writer).runtime.target))
        assert [statement.kind.value for statement in alone.statements] == [
            "merge",
            "verification",
        ]
        # Each statement carries the canonical envelope of exactly its own SQL and parameters.
        for statement in alone.statements:
            assert statement.start_envelope == compose_start_envelope(
                ComposeStartEnvelopeRequest(
                    workgroup="fictional-batch",
                    database=workload.database,
                    sql=statement.sql,
                    parameters=statement.parameters,
                )
            )
    world = _graph_batch_world(writer, entries=entries, inline=_BatchInline())
    result = world.process(receive_count=1)
    nodes = world.nodes()
    # The 10-row member that once needed 11 statements is shaped and settles like the others.
    assert [node.return_reason for node in nodes] == [None, None, None]
    assert [node.state.value for node in nodes] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert result.acknowledge is True
    assert reported == []


def test_slice7_4_one_poison_in_eight_is_returned_and_the_rest_settle(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    poison = _publication(5)
    world = _batch_world(writer, monkeypatch, 8, _BatchInline(poison=frozenset({poison})))
    batch = world.batch_id
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_batch_settled")
    states = _states(world)
    assert states[5] == "PENDING" and world.node(5).return_reason == "member_failed"
    assert [state for index, state in enumerate(states) if index != 5] == ["SUCCEEDED"] * 7
    # 1 + 2 * ceil(log2 8) = 7 set executions: the set, then two halves at each of 3 levels.
    assert len(world.inline.requests) == 7
    assert world.lock()["holder_batch_id"] == {"S": batch}
    assert world.open_members() == 0  # 4b: a poison never holds the table
    assert reported == []


def test_slice7_4_control_no_poison_is_one_set_and_no_split(
    writer: Any, monkeypatch: pytest.MonkeyPatch, exporter: Any
) -> None:
    world = _batch_world(writer, monkeypatch, 8)
    monkeypatch.setattr(writer, "RUNTIME", world.runtime)
    assert writer.handler(world.event(), _Context()) == {"batchItemFailures": []}
    assert len(world.inline.requests) == 1
    (batch,) = [s for s in exporter.get_finished_spans() if s.name.endswith("graph_unit_batch")]
    attributes = dict(batch.attributes or {})
    assert (attributes["split_count"], attributes["set_count"]) == (0, 1)
    assert attributes["succeeded_count"] == 8


def test_slice7_4_one_poison_on_the_final_attempt_fails_alone_and_alarms_once(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    poison = _publication(5)
    world = _batch_world(writer, monkeypatch, 8, _BatchInline(poison=frozenset({poison})))
    result = world.process(receive_count=MAX_RECEIVES)
    assert result.acknowledge is True
    node = world.node(5)
    assert (node.state.value, node.failure_reason) == ("FAILED", "statement_failed")
    assert world.run_record(5).fence_reason.value == "node_failed"
    assert [s for i, s in enumerate(_states(world)) if i != 5] == ["SUCCEEDED"] * 7
    (error,) = reported
    context = cast("Any", error).context
    assert str(cast("Any", error).code) == "ADLY-TABLE_WRITER-0006"
    assert (context["batch_id"], context["error_code"]) == (world.batch_id, "none")
    assert world.open_members() == 0


def test_slice7_4_a_systemic_failure_stops_after_three_executions(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    every = frozenset(_publication(index) for index in range(4))
    world = _batch_world(writer, monkeypatch, 4, _BatchInline(poison=every))
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert len(world.inline.requests) == 3
    assert _states(world) == ["CLAIMED"] * 4
    assert world.open_members() == 4
    # Control: on the final attempt the split continues to single members.
    final = _batch_world(writer, monkeypatch, 4, _BatchInline(poison=every))
    final.process(receive_count=MAX_RECEIVES)
    assert _states(final) == ["FAILED"] * 4
    assert len(final.inline.requests) == 7


def test_slice7_4_two_poisons_in_different_halves_are_systemic_until_final(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    poisons = frozenset({_publication(1), _publication(5)})
    world = _batch_world(writer, monkeypatch, 8, _BatchInline(poison=poisons))
    assert world.process(receive_count=1).outcome == "graph_unit_batch_unresolved"
    assert _states(world) == ["CLAIMED"] * 8
    # Control: on the final attempt both poisons are isolated and the rest succeed.
    final = _batch_world(writer, monkeypatch, 8, _BatchInline(poison=poisons))
    final.process(receive_count=MAX_RECEIVES)
    states = _states(final)
    assert (states[1], states[5]) == ("FAILED", "FAILED")
    assert [s for i, s in enumerate(states) if i not in {1, 5}] == ["SUCCEEDED"] * 6


def test_slice7_4b_after_a_poison_the_next_arm_pass_grants_the_table(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution_graph_release_support import release_store  # noqa: PLC0415

    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.instantiate import ingestion_node_plan  # noqa: PLC0415
    from adapters.jobs.execution_graph.promoter import (  # noqa: PLC0415
        PromoterContext,
        arm_pass,
    )

    world = _batch_world(writer, monkeypatch, 2, _BatchInline(poison=frozenset({_publication(1)})))
    world.process(receive_count=1)
    assert world.open_members() == 0
    counts = arm_pass(
        PromoterContext(release_store(world.graph), ingestion_node_plan, lambda: NOW),
        due_shards(),
        lambda: 60_000,
        "test-request",
    )
    assert (counts.batches_armed, counts.lock_wait) == (1, 0)
    assert world.node(1).state.value == "READY" and world.node(1).arm_alone
    # Control: a member left unresolved holds its share, and the next pass waits.
    held = _batch_world(writer, monkeypatch, 2, _BatchInline(script=["refused_table_busy"]))
    held.process(receive_count=1)
    assert held.open_members() == 2


def test_slice7_6_a_member_left_in_no_class_is_an_accounting_invariant(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain.shared_kernel.errors import GRAPH_UNIT_BATCH_ACCOUNTING_CODE  # noqa: PLC0415

    world = _batch_world(writer, monkeypatch, 2)

    def skip(_self: Any, _chosen: Any) -> None:
        return None

    monkeypatch.setattr(writer._Batch, "run_formed", skip)
    with pytest.raises(InvariantError) as refused:
        world.process()
    assert refused.value.code == GRAPH_UNIT_BATCH_ACCOUNTING_CODE
    assert _claim_item(world)["lease_expires_at"] == {"S": NOW.isoformat()}  # released first
    monkeypatch.undo()
    # Control: complete accounting acknowledges.
    assert _batch_world(writer, monkeypatch, 2).process().acknowledge is True


def test_slice7_6_an_exhausted_settlement_guard_reports_unsettled(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    from domain.operations.execution_graph_dispatch import (  # noqa: PLC0415
        NODE_STATE_CHANGED,
        GraphGuardError,
    )
    from domain.shared_kernel.errors import GRAPH_UNIT_BATCH_UNSETTLED_CODE  # noqa: PLC0415

    world = _batch_world(writer, monkeypatch, 2)

    def refuse(_request: Any) -> tuple[str, ...]:
        raise GraphGuardError(NODE_STATE_CHANGED)

    monkeypatch.setattr(world.runtime.graph, "settle_unit_set", refuse)
    result = world.process()
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    (error,) = reported
    assert cast("Any", error).code == GRAPH_UNIT_BATCH_UNSETTLED_CODE
    assert cast("Any", error).context["node_ids"] == [n.node_id for n in world.nodes()]
    # Control: a clean settlement reports nothing (test 1).


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_batch_owner(members=[]), id="no_members"),
        pytest.param(
            _batch_owner(members=[{"run_id": "r", "node_id": "n", "dispatch_id": "d"}] * 2),
            id="repeated_node_id",
        ),
        pytest.param(
            _batch_owner(
                members=[
                    {"run_id": "r", "node_id": f"n{index}", "dispatch_id": "d"}
                    for index in range(25)
                ]
            ),
            id="25_members",
        ),
        pytest.param(_batch_owner(lock_key="MATERIALIZE#u"), id="lock_outside_table"),
        pytest.param({**_unit_owner(), **_GRAPH_BINDING}, id="pre_1b_write_unit"),
        pytest.param({"batch_id": "b" * 64, **_body()}, id="batch_field_on_legacy"),
    ],
)
def test_slice7_10_a_malformed_batch_envelope_is_refused(
    writer: Any, body: dict[str, object]
) -> None:
    with pytest.raises(writer.GraphEnvelopeShapeError) as refused:
        writer.table_writer_delivery(_event(body=body))
    assert str(refused.value.code) == "ADLY-TABLE_WRITER-0004"
    assert refused.value.context["signal"] == "GRAPH_ENVELOPE_SHAPE_INVALID"


def test_slice7_10_control_one_and_three_member_batches_parse(writer: Any) -> None:
    three = [{"run_id": "r", "node_id": f"n{index}", "dispatch_id": "d"} for index in range(3)]
    for members in (three[:1], three):
        envelope = writer.table_writer_delivery(_event(body=_batch_owner(members=members)))
        assert len(envelope.members) == len(members)


def test_slice7_11_a_stale_member_is_skipped_with_zero_effect(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _batch_world(writer, monkeypatch, 2)
    world.graph.items[(f"RUN#{world.runs[1]}", f"NODE#write_unit:{UNIT_KEY}")]["batch_id"] = {
        "S": "0" * 64
    }
    result = world.process()
    assert result.acknowledge is True
    assert _states(world) == ["SUCCEEDED", "DISPATCHED"]
    assert world.inline.sets() == [{_publication(0)}]
    # Control: the matching member was claimed and executed.
    assert world.node(0).claim_record_present


def test_slice7_11_a_lost_member_drops_and_the_rest_rerun_in_a_new_set(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ports.outbound.projection_coordinator import (  # noqa: PLC0415
        GraphAuthorityRefusedError,
    )

    world = _batch_world(writer, monkeypatch, 3)

    calls: list[set[str]] = []
    inner = _BatchInline()

    def first_refused(runtime: Any, request: Any) -> Any:
        calls.append({identity for identity, _ in request.workload.records_by_identity})
        if len(calls) == 1:
            node = world.node(1)
            item = world.graph.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")]
            item["lease_version"] = {"N": str(int(item["lease_version"]["N"]) + 1)}
            raise GraphAuthorityRefusedError("GRAPH_CLAIM_SUPERSEDED")
        return inner(runtime, request)

    world.runtime = replace(world.runtime, inline_run=first_refused)
    result = world.process()
    assert result.acknowledge is True
    assert calls[1] == {_publication(0), _publication(2)}
    assert _states(world) == ["SUCCEEDED", "CLAIMED", "SUCCEEDED"]


def test_slice7_m3_one_outcome_is_counted_per_invocation(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted: list[str] = []

    def count(outcome: str, _receive_count: int) -> None:
        counted.append(outcome)

    monkeypatch.setattr(writer, "_count_receive", count)
    returned = _batch_world(
        writer, monkeypatch, 3, _BatchInline(poison=frozenset({_publication(2)}))
    )
    monkeypatch.setattr(writer, "RUNTIME", returned.runtime)
    writer.handler(returned.event(), _Context())
    unresolved = _batch_world(writer, monkeypatch, 3, _BatchInline(script=["refused_table_busy"]))
    monkeypatch.setattr(writer, "RUNTIME", unresolved.runtime)
    writer.handler(unresolved.event(), _Context())
    alone = _graph_unit_world(writer)
    monkeypatch.setattr(writer, "RUNTIME", alone.runtime)
    writer.handler(alone.event(), _Context())
    assert counted == [
        "graph_unit_batch_settled",
        "graph_unit_batch_unresolved",
        "graph_unit_succeeded",
    ]
    for outcome in (
        "graph_unit_returned",
        "graph_batch_member_stale",
        "graph_unit_claim_refused_live",
        "graph_unit_batch_settled",
        "graph_unit_batch_unresolved",
    ):
        assert outcome in writer.UNIT_RECEIVE_OUTCOMES


def test_slice7_m4_each_set_record_carries_rows_partitions_and_execution_ms(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[Any] = []
    real = writer.report_operation

    def capture(record: Any, logger: Any) -> None:
        logged.append(record)
        real(record, logger)

    monkeypatch.setattr(writer, "report_operation", capture)
    inline = _BatchInline(elapsed_ms=4_321)
    world = _batch_world(writer, monkeypatch, 2, inline)
    inline.clock = world.harness.clock
    world.process()
    (record,) = [item for item in logged if item.phase == "graph_unit_set_executed"]
    details = record.details
    assert (record.outcome, details["execution_ms"], details["statements"]) == (
        "finalized",
        4_321,
        2,
    )
    assert (details["rows"], details["max_partitions_per_merge"]) == (2, 2)
    assert details["batch_id"] == world.batch_id and details["deadline_cancelled"] is False


def test_slice7_13_one_batch_span_one_unit_span_per_member_and_each_metric_once(
    writer: Any, monkeypatch: pytest.MonkeyPatch, exporter: Any
) -> None:
    emitted: list[tuple[str, object]] = []

    class _Metric:
        def __init__(self, **kwargs: Any) -> None:
            emitted.append((kwargs["name"], kwargs["value"]))

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def add_dimension(self, **_kwargs: object) -> None:
            return None

        def add_metadata(self, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(writer, "single_metric", _Metric)
    world = _batch_world(writer, monkeypatch, 3)
    monkeypatch.setattr(writer, "RUNTIME", world.runtime)
    assert writer.handler(world.event(), _Context()) == {"batchItemFailures": []}
    spans = exporter.get_finished_spans()
    (batch,) = [s for s in spans if s.name == "ingest.table_writer.graph_unit_batch"]
    units = [s for s in spans if s.name == "ingest.table_writer.graph_unit"]
    assert len(units) == 3
    assert all(
        unit.parent is not None and unit.parent.span_id == batch.context.span_id for unit in units
    )
    assert {dict(unit.attributes or {})["batch_id"] for unit in units} == {world.batch_id}
    names = [name for name, _ in emitted if name.startswith("GraphUnitBatch")]
    assert sorted(names) == ["GraphUnitBatchCommitMs", "GraphUnitBatchSize", "GraphUnitBatchSplits"]
    assert dict(emitted)["GraphUnitBatchSize"] == 3


def test_slice7_n2_lost_member_reruns_and_live_redecides_share_one_counter(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adapters.inbound.ingestion_handlers.graph_claim import (  # noqa: PLC0415
        MAX_GUARD_REDECIDES,
    )
    from ports.outbound.projection_coordinator import (  # noqa: PLC0415
        GraphAuthorityRefusedError,
    )

    world = _batch_world(writer, monkeypatch, 2)
    calls: list[int] = []

    def refused(_runtime: Any, _request: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:  # the first refusal is a real takeover of member 1
            item = world.graph.items[(f"EXEC_CLAIM#{world.node(1).node_id}", "CLAIM")]
            item["lease_version"] = {"N": str(int(item["lease_version"]["N"]) + 1)}
        raise GraphAuthorityRefusedError("GRAPH_CLAIM_SUPERSEDED")

    world.runtime = replace(world.runtime, inline_run=refused)
    result = world.process()
    # One lost-member rerun plus live re-decides stop after max_redecide reruns in total.
    assert len(calls) == 1 + MAX_GUARD_REDECIDES
    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    # Control: the lost member was dropped (its lease untouched); the live one was released.
    lost = world.graph.items[(f"EXEC_CLAIM#{world.node(1).node_id}", "CLAIM")]
    live = world.graph.items[(f"EXEC_CLAIM#{world.node(0).node_id}", "CLAIM")]
    assert live["lease_expires_at"] == {"S": NOW.isoformat()}
    assert lost["lease_expires_at"] != {"S": NOW.isoformat()}


def _emf_batch_metrics(captured: str) -> list[dict[str, Any]]:
    """Parse the batch metrics' EMF lines printed by `single_metric`."""
    found: list[dict[str, Any]] = []
    for line in captured.splitlines():
        try:
            parsed: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        record = cast("dict[str, Any]", parsed)
        if any(name.startswith("GraphUnitBatch") for name in record):
            found.append(record)
    return found


def _metric_names(records: list[dict[str, Any]]) -> list[str]:
    return sorted(
        name for record in records for name in record if name.startswith("GraphUnitBatch")
    )


_BATCH_METRIC_NAMES: Final = [
    "GraphUnitBatchCommitMs",
    "GraphUnitBatchSize",
    "GraphUnitBatchSplits",
]


@pytest.mark.parametrize("fault", ["accounting_invariant", "store_unavailable"])
def test_slice7_f5_a_raised_batch_still_emits_each_metric_once_with_its_batch_id(  # noqa: PLR0913, PLR0917
    writer: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    """FEAT-009 review F5 and F6: D37's metrics survive an exception and name their batch."""
    from domain.shared_kernel.errors import GRAPH_UNIT_BATCH_ACCOUNTING_CODE  # noqa: PLC0415
    from ports.outbound.execution_graph_dispatch import (  # noqa: PLC0415
        GraphStoreUnavailableError,
    )

    world = _batch_world(writer, monkeypatch, 2)
    capsys.readouterr()
    if fault == "accounting_invariant":

        def skip(_self: Any, _chosen: Any) -> None:
            return None

        monkeypatch.setattr(writer._Batch, "run_formed", skip)
        with pytest.raises(InvariantError) as refused:
            world.process()
        assert refused.value.code == GRAPH_UNIT_BATCH_ACCOUNTING_CODE
    else:

        def unavailable(_run_id: str) -> Any:
            raise GraphStoreUnavailableError("injected read failure")

        monkeypatch.setattr(world.runtime.graph, "read_nodes", unavailable)
        with pytest.raises(GraphStoreUnavailableError):
            world.process()
    records = _emf_batch_metrics(capsys.readouterr().out)
    assert _metric_names(records) == _BATCH_METRIC_NAMES
    assert {record["batch_id"] for record in records} == {world.batch_id}
    # A property, never a dimension: no table or run dimension either (D37).
    for record in records:
        (directive,) = record["_aws"]["CloudWatchMetrics"]
        assert directive["Dimensions"] in ([], [[]])
    # Control: the same batch with no fault emits each metric once, with the same property.
    monkeypatch.undo()
    clean = _batch_world(writer, monkeypatch, 2)
    capsys.readouterr()
    assert clean.process().acknowledge is True
    settled = _emf_batch_metrics(capsys.readouterr().out)
    assert _metric_names(settled) == _BATCH_METRIC_NAMES
    assert {record["batch_id"] for record in settled} == {clean.batch_id}


# --- FEAT-009 review F7: the three invariants the first iteration left untested ---------------


def test_slice7_n1_a_formation_that_yields_no_set_is_an_invariant_with_zero_effect(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: an empty formation after step 2 is `GRAPH_UNIT_SET_COMPOSITION`, fault-injected.

    D10: `form_sets` composes nothing, so the fault enters through the grouping step.
    """
    from domain.shared_kernel.errors import GRAPH_UNIT_SET_COMPOSITION_CODE  # noqa: PLC0415

    world = _graph_unit_world(writer)

    def nothing(*_args: object) -> list[object]:
        return []

    monkeypatch.setattr(writer, "_group_members", nothing)
    with pytest.raises(InvariantError) as refused:
        world.process()
    assert refused.value.code == GRAPH_UNIT_SET_COMPOSITION_CODE
    assert refused.value.context["signal"] == "GRAPH_UNIT_SET_COMPOSITION"
    assert world.inline.requests == []
    assert world.node().state.value == "CLAIMED"
    assert _claim_item(world)["lease_expires_at"] == {"S": NOW.isoformat()}  # released first
    monkeypatch.undo()
    # Control: the real formation gives the same member a set of one, and it settles.
    assert _graph_unit_world(writer).process().outcome == "graph_unit_succeeded"


def test_slice7_5_a_crash_between_chunks_is_finished_by_the_redelivery(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 5: chunk 1 committed, then the invocation died; the redelivery settles the rest."""
    from adapters.outbound.dynamodb import execution_graph_dispatch as adapter  # noqa: PLC0415
    from ports.outbound.execution_graph_dispatch import (  # noqa: PLC0415
        GraphStoreUnavailableError,
    )

    monkeypatch.setattr(adapter, "SETTLE_CHUNK_MEMBERS", 1)  # one member per chunk
    world = _batch_world(writer, monkeypatch, 3)
    real = world.graph.transact_write_items
    settled_chunks: list[int] = []

    def crash_on_second_chunk(**kwargs: Any) -> Any:
        if '"SUCCEEDED"' in json.dumps(kwargs["TransactItems"]):
            settled_chunks.append(1)
            if len(settled_chunks) == 2:
                world.graph.fail_next = True
        return real(**kwargs)

    monkeypatch.setattr(world.graph, "transact_write_items", crash_on_second_chunk)
    with pytest.raises(GraphStoreUnavailableError):
        world.process(receive_count=1)
    assert _states(world) == ["SUCCEEDED", "CLAIMED", "CLAIMED"]
    assert world.open_members() == 2  # chunk 1 released its one share
    # The redelivery reads member 0 as settled and runs the other two as a new set.
    world.inline.requests.clear()
    result = world.process(receive_count=2)
    assert result.acknowledge is True
    assert world.inline.sets() == [{_publication(1), _publication(2)}]
    assert _states(world) == ["SUCCEEDED"] * 3
    assert world.open_members() == 0
    # Control: with no crash, one invocation acknowledges and frees the table.
    monkeypatch.undo()
    monkeypatch.setattr(adapter, "SETTLE_CHUNK_MEMBERS", 1)
    clean = _batch_world(writer, monkeypatch, 3)
    assert clean.process(receive_count=1).acknowledge is True
    assert (_states(clean), clean.open_members()) == (["SUCCEEDED"] * 3, 0)


def _rearm(world: _GraphUnitWorld) -> None:
    """Run the arm pass and the dispatcher again; the world's message is the new batch."""
    from execution_graph_dispatch_support import SpySender, now_text, store  # noqa: PLC0415
    from execution_graph_release_support import release_store  # noqa: PLC0415

    from adapters.jobs.execution_graph.dispatcher import (  # noqa: PLC0415
        DispatchContext,
        dispatch_shard,
    )
    from adapters.jobs.execution_graph.fence_settler import due_shards  # noqa: PLC0415
    from adapters.jobs.execution_graph.instantiate import ingestion_node_plan  # noqa: PLC0415
    from adapters.jobs.execution_graph.promoter import (  # noqa: PLC0415
        PromoterContext,
        arm_pass,
    )

    arm_pass(
        PromoterContext(release_store(world.graph), ingestion_node_plan, lambda: NOW),
        due_shards(),
        lambda: 60_000,
        "test-request",
    )
    sender = SpySender()
    for shard in due_shards():
        dispatch_shard(DispatchContext(store(world.graph), sender, now_text), shard)
    (sent,) = [body for body, _group, _dedup in sender.calls if '"write_unit_batch"' in body]
    world.sent = sent


@pytest.mark.parametrize("admission", [2, 3])
def test_slice7_4_f5_a_member_returned_by_the_time_rule_gets_exactly_one_more_attempt(  # noqa: PLR0913, PLR0917
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException], admission: int
) -> None:
    """Test 4 F5: `final_unisolated` at receive 3 leaves one solo attempt, then FAILED."""
    from domain.operations.statement_protocol import MIN_EXCLUSIVE_BUDGET_MS  # noqa: PLC0415

    world = _batch_world(writer, monkeypatch, 2)
    _remaining(world, MIN_EXCLUSIVE_BUDGET_MS + 1)  # the time rule refuses two members
    receives = [1] * (admission - 1) if admission == 3 else [1]
    for receive in receives:  # unresolved copies, each a re-admission
        assert world.process(receive_count=receive).outcome == "graph_unit_batch_unresolved"
    assert _claim_item(world)["admission"] == {"N": str(admission - 1)}
    world.process(receive_count=MAX_RECEIVES)
    assert _claim_item(world)["admission"] == {"N": str(admission)}
    first = world.node(0)
    assert (first.state.value, first.return_reason, first.final_return, first.arm_alone) == (
        "PENDING",
        "final_unisolated",
        True,
        True,
    )
    # Exactly one more attempt, armed alone (D41): a batch of one, where no time rule runs.
    _rearm(world)
    assert len(json.loads(world.sent)["members"]) == 1
    world.inline.script = ["insufficient_budget"]
    result = world.process(receive_count=1)
    assert (result.acknowledge, result.outcome) == (True, "graph_unit_failed")
    assert (world.node(0).state.value, world.node(0).failure_reason) == (
        "FAILED",
        "insufficient_budget",
    )
    assert world.node(0).return_reason == "final_unisolated"  # nothing returned it again
    assert [type(error).__name__ for error in reported] == ["GraphUnitFailedError"]


@pytest.mark.covers("ADR-149")
@pytest.mark.parametrize(
    "case",
    [
        ("semantic_v2_staging_database", "staging database"),
        ("semantic_v2_staging_bucket", "staging bucket"),
    ],
)
def test_the_writer_runtime_refuses_a_blank_staging_setting(
    writer: Any, monkeypatch: pytest.MonkeyPatch, case: tuple[str, str]
) -> None:
    field, label = case
    # Control: the module imported, so the runtime built with every staging setting present.
    assert writer.RUNTIME is not None
    monkeypatch.setattr(writer, "SETTINGS", writer.SETTINGS.model_copy(update={field: " "}))
    with pytest.raises(ValueError, match=label):
        writer._runtime()


# --- ADR-149 slice S3: staging before Begin, staged verification and attested telemetry -------


def _attested_line(writer: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    logged: list[Any] = []
    real = writer.report_operation

    def capture(log: Any, logger: Any) -> None:
        if log.operation == "semantic_unit_attested":
            logged.append(log)
        real(log, logger)

    monkeypatch.setattr(writer, "report_operation", capture)
    return logged


@pytest.mark.covers("ADR-149")
def test_t8_the_merge_stages_before_its_begin_and_the_verification_never_stages(
    writer: Any,
) -> None:
    harness = _harness(writer)
    harness.stager.calls = harness.calls

    result = harness.process()

    assert result.outcome == "attested"
    begins = [index for index, call in enumerate(harness.calls) if call == "begin_table_attempt"]
    assert harness.calls.count("stage") == 1  # the MERGE only, never the verification
    assert harness.calls.index("stage") < begins[0]
    (staged,) = harness.stager.ensured
    shaped = writer._shape_unit(harness.runtime, UNIT)
    assert staged.rows == sum(len(records) for _, records in shaped.workload.records_by_identity)
    # The staged body is the one the MERGE names: its digest is the key in the MERGE's SQL.
    merge = harness.start.requests[0]
    assert staged.staging_key in merge.start_envelope


@pytest.mark.covers("ADR-149")
def test_t8_a_failing_stage_begins_nothing_reserves_nothing_and_retries(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ports.outbound.unit_staging import StagingUnavailableError  # noqa: PLC0415

    failed_lines: list[Any] = []
    real = writer.report_operation

    def capture(log: Any, logger: Any) -> None:
        if log.operation == "semantic_statement_failed":
            failed_lines.append(log)
        real(log, logger)

    monkeypatch.setattr(writer, "report_operation", capture)
    harness = _harness(writer)
    harness.stager.failure = StagingUnavailableError("injected throttle")

    result = harness.process(receive_count=1)

    assert result == writer._UnitResult(acknowledge=False, outcome="statement_retryable")
    (line,) = failed_lines  # T-9: the staging failure is logged once, as retryable
    assert (line.details["retryable"], line.details["receive_count"]) == (True, 1)
    assert line.outcome == UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED.value
    assert "begin_table_attempt" not in harness.calls
    assert "start" not in harness.calls
    assert harness.world.unit_item is not None
    assert harness.world.unit_item.get("unit_reservation_count", 0) == 0
    assert harness.world.plan_reservation_count == 0
    assert harness.calls[-1] == "release_table_fence"
    # On the final receive the same failure fails the unit like an Athena outage.
    final = _harness(writer)
    final.stager.failure = StagingUnavailableError("injected throttle")
    failed = final.process(receive_count=MAX_RECEIVES)
    assert failed.outcome == "unit_failed"
    failure = next(r for r in final.coordinator.requests if isinstance(r, FailUnitRequest))
    assert failure.reason is UnitFailureReason.STATEMENT_RETRIES_EXHAUSTED
    # Control: a passing stage begins exactly once per statement.
    clean = _harness(writer)
    assert clean.process().outcome == "attested"
    assert clean.calls.count("begin_table_attempt") == 2


def _counted_unit_reads(harness: _Harness) -> list[str]:
    reads: list[str] = []
    real = harness.runtime.reader.unit_item

    def counted(plan_key: str, unit_key: str) -> dict[str, object] | None:
        reads.append(unit_key)
        return real(plan_key, unit_key)

    harness.runtime.reader.unit_item = counted
    return reads


@pytest.mark.covers("ADR-149")
def test_t9_the_attested_line_carries_two_accepted_attempts_one_merge_and_its_rows(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Baseline: the same receive without the count read, to pin exactly one extra read.
    baseline = _harness(writer)
    baseline_reads = _counted_unit_reads(baseline)
    with monkeypatch.context() as patched:

        def unread(_ctx: object) -> None:
            return None

        patched.setattr(writer, "_accepted_attempts", unread)
        assert baseline.process().outcome == "attested"
    logged = _attested_line(writer, monkeypatch)
    harness = _harness(writer)
    reads = _counted_unit_reads(harness)

    assert harness.process().outcome == "attested"

    (line,) = logged
    shaped = writer._shape_unit(harness.runtime, UNIT)
    rows = sum(len(records) for _, records in shaped.workload.records_by_identity)
    assert (line.details["accepted_attempts"], line.details["merge_count"]) == (2, 1)
    assert line.details["rows"] == rows
    assert harness.world.unit_item is not None
    assert line.details["accepted_attempts"] == harness.world.unit_item["unit_reservation_count"]
    assert len(reads) == len(baseline_reads) + 1


@pytest.mark.covers("ADR-149")
def test_t9_a_failed_count_read_still_attests_with_a_null_count_reported_once(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged = _attested_line(writer, monkeypatch)
    errors: list[BaseException] = []

    def record(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(writer, "report_error", record)
    real = writer._snapshot_count

    def failing(item: Any, name: str) -> int:
        if name == "unit_reservation_count" and item.get("status") == "attested":
            raise DependencyError(ErrorCode("FICTIONAL", 3), "injected count read")
        return real(item, name)

    monkeypatch.setattr(writer, "_snapshot_count", failing)
    harness = _harness(writer)

    result = harness.process()

    assert (result.acknowledge, result.outcome) == (True, "attested")
    (line,) = logged
    assert line.details["accepted_attempts"] is None
    assert [type(error).__name__ for error in errors] == ["DependencyError"]


def _reconcile_parked(world: _World) -> None:
    """Apply the reconciler's effect on a parked attempt: complete it, clear fence and lease.

    It completes an existing attempt and reserves none (§4.8), so the unit's stored
    reservation count is untouched.
    """
    fence = world.fence
    assert fence is not None and fence.state == "parked"
    attempt_id = str(fence.attempt_id)
    world.attempts[attempt_id] = replace(
        world.attempts[attempt_id],
        state=ProviderTerminalState.CANCELLED.value,
        athena_usage_state="unsettled",
    )
    world.fence = None
    world.lease = None
    unit = world.unit_item
    assert unit is not None
    references = cast("list[object]", unit.get("attempt_references", []))
    unit["attempt_references"] = [*references, attempt_id]
    unit["ordinal"] = int(str(unit["ordinal"])) + 1
    del unit["current_attempt_id"]


@pytest.mark.covers("ADR-149")
def test_t9_a_park_at_the_verification_makes_the_next_receive_attest_with_four_attempts(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepted attempts accumulate across receives; §4.8's `2 <= n <= 2(k+1)` rule needs it."""
    logged: list[Any] = []
    real = writer.report_operation

    def capture(log: Any, logger: Any) -> None:
        if log.operation in {"semantic_unit_attested", "semantic_attempt_parked"}:
            logged.append(log)
        real(log, logger)

    monkeypatch.setattr(writer, "report_operation", capture)
    first = _harness(writer)
    merge_terminal = first.terminals.observe

    def verification_never_ends(query_execution_id: str) -> ObserveTableAttemptResult:
        if query_execution_id == "q-2":  # the verification runs past the safety deadline
            first.calls.append("observe")
            return ProviderNonterminal(reason="nonterminal")
        return merge_terminal(query_execution_id)

    def stop_ignored(query_execution_id: str) -> None:
        assert query_execution_id == "q-2"

    first.terminals.observe = verification_never_ends  # type: ignore[method-assign]
    first.terminals.stop = stop_ignored  # type: ignore[method-assign]

    assert first.process(receive_count=1).outcome == "attempt_parked"
    begins = [r for r in first.coordinator.requests if isinstance(r, BeginTableAttemptRequest)]
    # The park is at the verification: both statements reached Begin on this receive.
    assert [begin.statement_kind for begin in begins] == ["merge", "verification"]
    world = first.world
    assert world.unit_item is not None and world.unit_item["unit_reservation_count"] == 2
    _reconcile_parked(world)
    second = _harness(writer, world)

    assert second.process(receive_count=2).outcome == "attested"

    parked, attested = logged
    assert (parked.operation, parked.details["receive_count"]) == ("semantic_attempt_parked", 1)
    assert parked.details["unit_key"] == attested.details["unit_key"] == UNIT_KEY
    assert (attested.operation, attested.details["receive_count"]) == ("semantic_unit_attested", 2)
    # Statements that reached Begin, summed over receives: 2 on the parked receive, 2 on the clean.
    assert attested.details["accepted_attempts"] == 4
    assert attested.details["accepted_attempts"] == world.unit_item["unit_reservation_count"]
    # Control: a clean first receive attests at 2, with no parked line.
    logged.clear()
    assert _harness(writer).process(receive_count=1).outcome == "attested"
    (clean,) = logged
    assert (clean.operation, clean.details["accepted_attempts"]) == ("semantic_unit_attested", 2)


@pytest.mark.covers("ADR-149")
def test_t9_a_verification_count_mismatch_retries_while_receives_remain(writer: Any) -> None:
    harness = _harness(writer)
    exact = harness.terminals.readback
    harness.terminals.readback = tuple(
        (side, identity, "0") if side == "matched_target" else (side, identity, count)
        for side, identity, count in exact
    )

    result = harness.process(receive_count=1)

    assert result == writer._UnitResult(acknowledge=False, outcome="statement_retryable")
    assert "finalize_unit" not in harness.calls
    # Control: the exact count rows attest.
    assert _harness(writer).process().outcome == "attested"


# --- ADR-149 slice S3, D10: at most one composed GRAPH set is live (T-11) ----------------------


class _Tracked(dict[str, Any]):
    """A staged record a weakref can follow; a plain dict can't be weakly referenced."""


@dataclass
class _D10:
    """Spies on re-shapes, compositions, groupings and runs, and on which sets stay alive.

    The run stub keeps no request, so a set's records are reachable only through the writer.
    `live_at_shape` records, at every re-shape after formation, how many earlier runs' records
    are still alive: D10 requires 0.
    """

    events: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    refs: list[Any] = field(default_factory=list[Any])
    live_at_shape: list[int] = field(default_factory=list[int])
    script: list[Any] = field(default_factory=list[Any])
    shape_failures: dict[int, BaseException] = field(default_factory=dict[int, BaseException])
    groups: list[Any] = field(default_factory=list[Any])
    formed: bool = False

    def after_form(self) -> list[tuple[str, Any]]:
        index = next(i for i, event in enumerate(self.events) if event[0] == "form")
        return self.events[index + 1 :]

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.after_form()]

    def __call__(self, runtime: Any, request: Any) -> Any:
        from adapters.inbound.ingestion_handlers.inline_run import (  # noqa: PLC0415
            InlineRunResult,
        )

        del runtime
        request.graph_claims()
        identities = frozenset(identity for identity, _ in request.workload.records_by_identity)
        self.events.append(("run", identities))
        import weakref  # noqa: PLC0415

        self.refs.append(weakref.ref(request.workload.records_by_identity[0][1][0]))
        outcome = self.script.pop(0) if self.script else "finalized"
        if isinstance(outcome, _LazyError):
            raise outcome.build()
        if isinstance(outcome, BaseException):
            raise outcome
        return InlineRunResult(
            outcome=outcome,
            run_id="inline-run-fictional",
            retryable=False,
            flow_id=STAGE1_FLOW,
            execution_id="88888888-8888-4888-8888-888888888888",
            predecessor_execution_id=None,
        )


def _d10_world(  # noqa: PLR0913, PLR0917 - one fixture builder
    writer: Any, monkeypatch: pytest.MonkeyPatch, entries: tuple[int, ...], spy: _D10
) -> _GraphUnitWorld:
    _batch_shaper(writer, monkeypatch)
    world = _graph_batch_world(
        writer, entries=tuple(_unit_entry_for(index) for index in entries), inline=spy
    )
    shaped_by = writer._shape_unit
    composer = writer.compose_unit_statements
    grouping = writer._group_members

    def shape(runtime: Any, entry: CanonicalUnitEntry) -> Any:
        publication = entry.objects[0].publication_id
        if spy.formed:
            spy.live_at_shape.append(sum(ref() is not None for ref in spy.refs))
            calls = sum(1 for kind, _ in spy.after_form() if kind == "shape")
            spy.events.append(("shape", publication))
            failure = spy.shape_failures.get(calls)
            if failure is not None:
                raise failure
        shaped = shaped_by(runtime, entry)
        records = tuple(
            (identity, tuple(_Tracked(record) for record in rows))
            for identity, rows in shaped.workload.records_by_identity
        )
        return replace(shaped, workload=replace(shaped.workload, records_by_identity=records))

    def compose(request: Any) -> Any:
        if spy.formed:
            identities = frozenset(i for i, _ in request.workload.records_by_identity)
            spy.events.append(("compose", identities))
        return composer(request)

    def group(*args: Any) -> Any:
        spy.formed = True
        spy.events.append(("form", None))
        groups = grouping(*args)
        spy.groups.extend(groups)
        return groups

    monkeypatch.setattr(writer, "_shape_unit", shape)
    monkeypatch.setattr(writer, "compose_unit_statements", compose)
    monkeypatch.setattr(writer, "_group_members", group)
    return world


@pytest.mark.covers("ADR-149")
def test_t11_a_b_c_f_each_group_composes_just_before_its_run_and_then_dies(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    spy = _D10()
    world = _d10_world(writer, monkeypatch, (0, 1, 1, 1), spy)

    result = world.process()

    assert result.acknowledge is True
    pub0, pub1 = _publication(0), _publication(1)
    # (b) formation returns member tuples and composes nothing; (c) per group: re-shape each
    # member, compose the union, run, and only then the next group's first re-shape.
    assert all(isinstance(group, tuple) for group in spy.groups)
    assert [len(group) for group in spy.groups] == [2, 1, 1]
    assert spy.after_form() == [
        ("shape", pub0),
        ("shape", pub1),
        ("compose", frozenset({pub0, pub1})),
        ("run", frozenset({pub0, pub1})),
        ("shape", pub1),
        ("compose", frozenset({pub1})),
        ("run", frozenset({pub1})),
        ("shape", pub1),
        ("compose", frozenset({pub1})),
        ("run", frozenset({pub1})),
    ]
    assert spy.live_at_shape == [0, 0, 0, 0]  # no earlier set is alive at any re-shape
    assert all(ref() is None for ref in spy.refs)
    # (f) after step 2 every member holds a body-free summary and no workload.
    for member in (m for group in spy.groups for m in group):
        assert isinstance(member.summary, writer._MemberSummary)
        assert not hasattr(member, "shaped")
    assert _states(world) == ["SUCCEEDED"] * 4
    assert reported == []


@pytest.mark.covers("ADR-149")
def test_t11_a_the_union_is_one_merge_over_every_members_rows(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _batch_world(writer, monkeypatch, 3)
    assert world.process().acknowledge is True
    (request,) = world.inline.requests
    merge = request.composition.merges[0]
    assert [statement.kind.value for statement in request.composition.statements] == [
        "merge",
        "verification",
    ]
    assert merge.staged is not None
    assert dict(merge.staged.row_counts) == {_publication(index): 1 for index in range(3)}


@pytest.mark.covers("ADR-149")
def test_t11_d_a_split_composes_each_half_only_after_the_previous_set_is_gone(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _D10(script=["statement_failed"])
    world = _d10_world(writer, monkeypatch, (0, 1, 2, 3), spy)

    assert world.process().acknowledge is True

    assert (
        spy.kinds() == ["shape"] * 4 + ["compose", "run"] + (["shape"] * 2 + ["compose", "run"]) * 2
    )
    assert spy.live_at_shape == [0] * 8  # the parent set is dead when the first half shapes
    assert _states(world) == ["SUCCEEDED"] * 4


@pytest.mark.covers("ADR-149")
def test_t11_d_a_table_stop_inside_a_split_composes_nothing_more(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = DependencyError(ErrorCode("FICTIONAL", 5), "injected run dependency")
    spy = _D10(script=["statement_failed", stop])
    world = _d10_world(writer, monkeypatch, (0, 1, 2, 3), spy)

    result = world.process(receive_count=1)

    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert spy.kinds() == ["shape"] * 4 + ["compose", "run"] + ["shape"] * 2 + ["compose", "run"]
    assert _states(world) == ["CLAIMED"] * 4  # every member has a class: unresolved retries


@pytest.mark.covers("ADR-149")
def test_t11_d1_a_first_half_reshape_dependency_error_stops_the_second_half_uncomposed(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _D10(script=["statement_failed"])
    spy.shape_failures[4] = DependencyError(ErrorCode("FICTIONAL", 6), "injected object read")
    world = _d10_world(writer, monkeypatch, (0, 1, 2, 3), spy)

    result = world.process(receive_count=1)

    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert spy.kinds() == ["shape"] * 4 + ["compose", "run", "shape"]
    assert _states(world) == ["CLAIMED"] * 4


@pytest.mark.covers("ADR-149")
def test_t11_d2_a_first_half_that_fails_its_checksums_lets_the_second_half_run(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _D10(script=["statement_failed"])
    spy.shape_failures[4] = writer._ChecksumMismatchError("injected")
    spy.shape_failures[5] = writer._ChecksumMismatchError("injected")
    world = _d10_world(writer, monkeypatch, (0, 1, 2, 3), spy)

    assert world.process(receive_count=1).acknowledge is True

    assert spy.kinds() == ["shape"] * 4 + ["compose", "run"] + ["shape"] * 4 + [
        "compose",
        "run",
    ]
    assert _states(world) == ["FAILED", "FAILED", "SUCCEEDED", "SUCCEEDED"]
    assert {world.node(i).failure_reason for i in (0, 1)} == {"checksum_mismatch"}


@pytest.mark.covers("ADR-149")
def test_t11_g_a_reshape_dependency_error_halts_the_set_with_no_split(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _D10()
    spy.shape_failures[0] = DependencyError(ErrorCode("FICTIONAL", 7), "injected object read")
    world = _d10_world(writer, monkeypatch, (0, 1), spy)

    result = world.process(receive_count=1)

    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert spy.kinds() == ["shape"]
    assert _states(world) == ["CLAIMED", "CLAIMED"]
    # Control: a checksum mismatch at re-shape fails only that member; the other runs alone.
    other = _D10()
    other.shape_failures[0] = writer._ChecksumMismatchError("injected")
    alone = _d10_world(writer, monkeypatch, (0, 1), other)
    assert alone.process(receive_count=1).acknowledge is True
    assert [event for event in other.after_form() if event[0] == "run"] == [
        ("run", frozenset({_publication(1)}))
    ]
    assert _states(alone) == ["FAILED", "SUCCEEDED"]


@pytest.mark.covers("ADR-149")
def test_t11_h_a_stager_failure_halts_the_set_table_and_never_splits(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ports.outbound.unit_staging import StagingUnavailableError  # noqa: PLC0415

    records: list[Any] = []
    real = writer.report_operation

    def capture(log: Any, logger: Any) -> None:
        if log.phase == "graph_unit_set_executed":
            records.append(log)
        real(log, logger)

    monkeypatch.setattr(writer, "report_operation", capture)
    spy = _D10(script=[StagingUnavailableError("injected throttle")])
    world = _d10_world(writer, monkeypatch, (0, 1), spy)

    result = world.process(receive_count=1)

    assert (result.acknowledge, result.outcome) == (False, "graph_unit_batch_unresolved")
    assert [record.outcome for record in records] == ["raised"]
    assert spy.kinds().count("run") == 1  # never split or isolated
    assert _states(world) == ["CLAIMED", "CLAIMED"]


@pytest.mark.covers("ADR-149")
def test_t11_i_a_refused_begin_drops_the_refused_set_before_reshaping_the_rest(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ports.outbound.projection_coordinator import (  # noqa: PLC0415
        GraphAuthorityRefusedError,
    )

    spy = _D10()
    world = _d10_world(writer, monkeypatch, (0, 1, 2), spy)
    node = world.node(1)

    def lose_one() -> GraphAuthorityRefusedError:
        item = world.graph.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")]
        item["lease_version"] = {"N": str(int(item["lease_version"]["N"]) + 1)}
        return GraphAuthorityRefusedError("GRAPH_CLAIM_SUPERSEDED")

    spy.script = [_LazyError(lose_one)]
    assert world.process().acknowledge is True
    assert spy.kinds() == ["shape"] * 3 + ["compose", "run"] + ["shape"] * 2 + ["compose", "run"]
    assert spy.live_at_shape == [0] * 5  # the refused set is dead when `remaining` re-shapes
    assert _states(world) == ["SUCCEEDED", "CLAIMED", "SUCCEEDED"]
    # Control: a refusal that loses no member re-runs the same set with no re-shape.
    again = _D10(script=[GraphAuthorityRefusedError("GRAPH_CLAIM_SUPERSEDED")])
    same = _d10_world(writer, monkeypatch, (0, 1), again)
    assert same.process().acknowledge is True
    assert again.kinds() == ["shape"] * 2 + ["compose", "run", "run"]


class _LazyError(Exception):
    """A scripted outcome built when it is reached, so a fault can change state first."""

    def __init__(self, build: Any) -> None:
        super().__init__("lazy")
        self.build = build


# --- ADR-149 slice S4: the unit row ceiling in GRAPH formation (T-11 e) ----------------------


def _row_ceiling(writer: Any, monkeypatch: pytest.MonkeyPatch, rows: int) -> None:
    monkeypatch.setattr(
        writer, "WRITER_SETTINGS", replace(writer.WRITER_SETTINGS, max_rows_per_unit=rows)
    )


@pytest.mark.covers("ADR-149")
def test_t11e_a_union_past_the_row_ceiling_starts_a_new_group(
    writer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _batch_world(writer, monkeypatch, 3)
    _row_ceiling(writer, monkeypatch, 2)  # each member has one row

    assert world.process().acknowledge is True

    assert world.inline.sets() == [{_publication(0), _publication(1)}, {_publication(2)}]
    assert _states(world) == ["SUCCEEDED"] * 3
    # Control: at a ceiling equal to the union's rows the three members form one group.
    control = _batch_world(writer, monkeypatch, 3)
    _row_ceiling(writer, monkeypatch, 3)
    assert control.process().acknowledge is True
    assert control.inline.sets() == [{_publication(0), _publication(1), _publication(2)}]


@pytest.mark.covers("ADR-149")
def test_slice7_ar5_3_row_cap_a_member_alone_over_the_row_ceiling_is_its_own_failure(
    writer: Any, monkeypatch: pytest.MonkeyPatch, reported: list[BaseException]
) -> None:
    _batch_shaper(writer, monkeypatch, rows={1: 10, 2: 9})
    entries = tuple(_unit_entry_for(index) for index in range(3))
    world = _graph_batch_world(writer, entries=entries, inline=_BatchInline())
    _row_ceiling(writer, monkeypatch, 9)

    result = world.process(receive_count=1)

    nodes = world.nodes()
    assert [node.state.value for node in nodes] == ["SUCCEEDED", "FAILED", "SUCCEEDED"]
    assert nodes[1].failure_reason == "unit_rows_exceed_cap"
    assert nodes[1].return_reason is None  # settled final on this receive, never member_failed
    # Control: the 9-row member at the ceiling is shaped and runs (alone: 1 + 9 > 9 rows).
    assert world.inline.sets() == [{_publication(0)}, {_publication(2)}]
    assert result.acknowledge is True
    assert len(reported) == 1  # the one final failure is alarmed once
