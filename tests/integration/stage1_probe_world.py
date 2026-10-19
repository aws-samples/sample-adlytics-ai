# SPDX-License-Identifier: MIT-0
"""Probe world for the ADR-075 Stage-1 Slice A live coordinator probes.

Every identity a probe invents carries ``integration_probe``/``integration-probe`` where the
key grammar allows it (physical table ids, dataset partitions, manifest keys, operation ids);
the digest-shaped keys (``WRITE_PLAN#``, ``STATUS_FLOW#``, ``PROVIDER_RECONCILE_BLOCK#``) are
derived from those probe inputs and are found again only through the recording client, which
captures every key it wrote so the session teardown can delete exactly those keys.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from boto3.dynamodb.types import TypeSerializer

from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import (
    Stage1WriterAuthorityReader,
    unit_entry_from_item,
)
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from domain.operations.stage1_lifecycle_head import (
    Stage1CompoundReplacementRequest,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    compound_replacement_proof,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    EvaluationBinding,
    InlineIdentityRequest,
    LegitimateRootSource,
    OwnerDisposition,
    PlanAdmissionSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    RootDiscriminator,
    SameTransactionTerminalSource,
    SettleStage1AthenaUsageRequest,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_inline_identity,
    derive_plan_identity,
    derive_unit_identity,
)
from domain.operations.stage1_operational_status import (
    ProviderTerminalState as StatusProviderTerminalState,
)
from domain.operations.stage1_operational_status import (
    RequiredChildExpectation as StatusChildExpectation,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    CompletionOutcome,
    FlowState,
    canonical_authority_digest,
)
from domain.operations.table_attempt import (
    AttemptObservation,
    FenceObservation,
    ProviderErrorClass,
    ProviderTerminalState,
    RunIdentityRequest,
    SanitizedStartEvidence,
    WriterKind,
    run_identity,
)
from domain.operations.table_lease import (
    TableLeaseRecord,
    inline_lease_holder,
    queued_lease_holder,
)
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    PlanMeta,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    SettledAttemptReference,
    SuccessorOwner,
    internal_execution_identity,
    plan_digest_for_source_delivery,
    plan_partition_key,
    source_delivery_identity,
)
from ports.outbound.projection_coordinator import (
    AcquireTableFenceRequest,
    ActivePlanObservation,
    AdmitUnitRequest,
    AthenaAttemptSettlement,
    AttemptBudgetExhaustion,
    AttestedUnitObservation,
    BeginInlineRunOwner,
    BeginQueuedUnitOwner,
    BeginTableAttemptRequest,
    BeginTableAttemptResult,
    ClaimPlanIntentRequest,
    ClaimUnitRequest,
    CompletePlanRequest,
    CompleteTableAttemptRequest,
    CurrentPointerOutcome,
    FailUnitRequest,
    FinalizeInlineRequest,
    FinalizeUnitRequest,
    InlineRunAttemptOwner,
    PersistInlineRunRequest,
    PersistPlanRequest,
    PlanHeadObservation,
    PointerOutcomeKind,
    QueuedUnitAttemptOwner,
    RecordStage1RefusalRequest,
    RefusedInlineRunObservation,
    RefusedUnitObservation,
    ReleaseTableFenceRequest,
    SettledAttemptObservation,
    SourcePlanReplacementEvidence,
    Stage1RefusalReason,
    Stage1RefusalTag,
    Stage1SettlementExpansion,
    SubmitPlanIntentRequest,
    UnitFailureReason,
    UnitFlowBinding,
)
from ports.outbound.table_attempt import RecordSubmittedAttemptRequest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

    from domain.operations.stage1_lifecycle_head import Stage1LifecycleHeadTuple

__all__ = [
    "CHECKSUM",
    "PROBE_TOKENS",
    "InlineWorld",
    "PlanWorld",
    "ProbeClock",
    "ProbeContext",
    "ProbeIdentity",
    "RecordingDynamo",
    "UnitWorld",
    "instant_to_datetime",
]

type PlainItem = dict[str, object]
type LowItem = dict[str, "AttributeValueTypeDef"]

PROBE_TOKENS: tuple[str, str] = ("integration_probe", "integration-probe")
"""Both spellings the key grammar forces on probe identities; the sweep matches either."""

CHECKSUM = "0123456789abcdef" * 4
"""A fictional immutable content checksum (64 lowercase hex), never a live value."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SERIALIZER = TypeSerializer()
_STATEMENT_KIND = "merge"
_UNIT_MEASUREMENT = "semantic_projection_unit"


def instant_to_datetime(instant: str) -> datetime:
    """Render a 20-digit epoch-microseconds instant as its UTC moment."""
    return _EPOCH + timedelta(microseconds=int(instant))


def _low_item(item: Mapping[str, object]) -> LowItem:
    return {
        name: cast("AttributeValueTypeDef", _SERIALIZER.serialize(value))
        for name, value in item.items()
    }


def _key_text(key: Mapping[str, Mapping[str, object]], name: str) -> str | None:
    value = key.get(name)
    if value is None:
        return None
    text = value.get("S")
    return text if isinstance(text, str) else None


# --- Recording client -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedKey:
    """One key this session wrote: table plus its complete primary key text."""

    table: str
    partition_key: str
    record_key: str | None

    def low(self) -> LowItem:
        key: LowItem = {"partition_key": {"S": self.partition_key}}
        if self.record_key is not None:
            key["record_key"] = {"S": self.record_key}
        return key


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """What the teardown deleted, what it could not prove gone, and what the sweep found."""

    recorded: int
    deleted: int
    residual: tuple[RecordedKey, ...]
    swept: int


class RecordingDynamo:
    """A real DynamoDB client that remembers every key it wrote and counts every API call.

    Only successful writes are recorded: a cancelled ``TransactWriteItems`` mutates nothing,
    so its keys are not this session's to delete. ``ConditionCheck`` members write nothing.
    """

    def __init__(self, client: DynamoDBClient, publications: str, leases: str) -> None:
        self._client = client
        self._publications = publications
        self._leases = leases
        self._written: dict[RecordedKey, None] = {}
        self.calls: Counter[str] = Counter()
        self.transactions: list[dict[str, Any]] = []

    @property
    def written(self) -> tuple[RecordedKey, ...]:
        """Every key written so far, in first-write order."""
        return tuple(self._written)

    def written_under(self, table: str, partition_prefix: str) -> tuple[RecordedKey, ...]:
        """Return the recorded keys of one table whose partition begins with a prefix."""
        return tuple(
            key
            for key in self._written
            if key.table == table and key.partition_key.startswith(partition_prefix)
        )

    def _remember(self, table: str, key: Mapping[str, Mapping[str, object]]) -> None:
        partition = _key_text(key, "partition_key")
        if partition is None:
            raise AssertionError("a written key carries a text partition_key.")
        record = _key_text(key, "record_key")
        self._written[RecordedKey(table, partition, record)] = None

    # -- the surface the adapters call ------------------------------------------------------------

    def get_item(self, **request: Any) -> Any:
        self.calls["GetItem"] += 1
        return self._client.get_item(**request)

    def query(self, **request: Any) -> Any:
        self.calls["Query"] += 1
        return self._client.query(**request)

    def transact_write_items(self, **request: Any) -> Any:
        self.calls["TransactWriteItems"] += 1
        self.transactions.append(request)
        response = self._client.transact_write_items(**request)
        for member in cast("list[dict[str, Any]]", request["TransactItems"]):
            if "Put" in member:
                body = cast("dict[str, Any]", member["Put"])
                self._remember(body["TableName"], cast("Mapping[str, Any]", body["Item"]))
            elif "Update" in member:
                body = cast("dict[str, Any]", member["Update"])
                self._remember(body["TableName"], cast("Mapping[str, Any]", body["Key"]))
        return response

    def update_item(self, **request: Any) -> Any:
        self.calls["UpdateItem"] += 1
        response = self._client.update_item(**request)
        self._remember(request["TableName"], cast("Mapping[str, Any]", request["Key"]))
        return response

    # -- probe seeding and teardown ----------------------------------------------------------------

    def put_probe_item(self, table: str, item: Mapping[str, object]) -> None:
        """Create one probe item this session owns; refuses to overwrite anything."""
        self.calls["PutItem"] += 1
        self._client.put_item(
            TableName=table,
            Item=_low_item(item),
            ConditionExpression="attribute_not_exists(partition_key)",
        )
        self._remember(table, _low_item(item))

    def read_plain(self, key: RecordedKey) -> dict[str, Any] | None:
        response = self._client.get_item(TableName=key.table, Key=key.low(), ConsistentRead=True)
        item = response.get("Item")
        return None if item is None else dict(item)

    def cleanup(self) -> CleanupReport:
        """Delete exactly the recorded keys, prove each gone, then sweep any probe token."""
        recorded = tuple(self._written)
        deleted = 0
        residual: list[RecordedKey] = []
        for key in recorded:
            self._client.delete_item(TableName=key.table, Key=key.low())
            deleted += 1
        for key in recorded:
            if self.read_plain(key) is not None:
                residual.append(key)
        self._written.clear()
        return CleanupReport(len(recorded), deleted, tuple(residual), self.sweep_probe_tokens())

    def sweep_probe_tokens(self) -> int:
        """Delete every item in either table whose key text contains a probe token."""
        removed = 0
        for table, has_record in ((self._publications, True), (self._leases, False)):
            for key in self._scan_probe_keys(table, has_record=has_record):
                self._client.delete_item(TableName=table, Key=key.low())
                removed += 1
        return removed

    def _scan_probe_keys(self, table: str, *, has_record: bool) -> Iterator[RecordedKey]:
        clauses = ["contains(partition_key, :a)", "contains(partition_key, :b)"]
        projection = "partition_key"
        if has_record:
            clauses.extend(("contains(record_key, :a)", "contains(record_key, :b)"))
            projection += ", record_key"
        paginator = self._client.get_paginator("scan")
        pages = paginator.paginate(
            TableName=table,
            ProjectionExpression=projection,
            FilterExpression=" OR ".join(clauses),
            ExpressionAttributeValues={
                ":a": {"S": PROBE_TOKENS[0]},
                ":b": {"S": PROBE_TOKENS[1]},
            },
        )
        for page in pages:
            for item in page.get("Items", []):
                partition = _key_text(cast("Mapping[str, Any]", item), "partition_key")
                if partition is None:
                    continue
                record = _key_text(cast("Mapping[str, Any]", item), "record_key")
                yield RecordedKey(table, partition, record if has_record else None)


# --- Clock and identities -------------------------------------------------------------------------


class ProbeClock:
    """Strictly increasing canonical instants, one millisecond apart, anchored at wall time."""

    def __init__(self) -> None:
        self._micros = int(time.time() * 1_000_000)

    @property
    def micros(self) -> int:
        return self._micros

    def instant(self) -> str:
        """Advance one millisecond and render the 20-digit epoch-microseconds instant."""
        self._micros += 1_000
        return f"{self._micros:020d}"

    def moment(self) -> datetime:
        return _EPOCH + timedelta(microseconds=self._micros)

    def lease_expiry(self, seconds: int) -> str:
        """Render a whole-second lease expiry ``seconds`` after now, as the writer does."""
        return (self.moment().replace(microsecond=0) + timedelta(seconds=seconds)).isoformat()


@dataclass(frozen=True, slots=True)
class ProbeIdentity:
    """Every invented identity, derived from the pytest node id so leftovers name their test."""

    node: str

    @property
    def suffix(self) -> str:
        return hashlib.sha256(self.node.encode()).hexdigest()[:12]

    def table(self, tag: str) -> str:
        return f"integration_probe_db.integration_probe_{tag}_{self.suffix}"

    def dataset_partition(self, ordinal: int, tag: str = "unit") -> str:
        return f"integration_probe.dataset|{tag}-{self.suffix}|p{ordinal:02d}"

    def manifest(self, tag: str) -> tuple[str, str]:
        return (f"integration-probe/{self.suffix}/{tag}.json", "integration-probe-v1")

    def operation_id(self, tag: str) -> str:
        return f"integration-probe-op-{tag}-{self.suffix}"

    def execution_id(self, tag: str) -> str:
        return f"integration-probe-exec-{tag}-{self.suffix}"

    def writer_execution(self, tag: str) -> str:
        return canonical_authority_digest(
            ["stage1-writer-execution/v1", f"integration-probe-message-{tag}-{self.suffix}", 1]
        )

    def query_execution_id(self, tag: str) -> str:
        return f"integration-probe-query-{tag}-{self.suffix}"


@dataclass
class ProbeContext:
    """The shared live adapters over the recording client plus one clock per test."""

    identity: ProbeIdentity
    dynamo: RecordingDynamo
    publications: str
    leases: str
    clock: ProbeClock = field(default_factory=ProbeClock)

    def __post_init__(self) -> None:
        client = cast("Any", self.dynamo)
        self.head_store = DynamoStage1LifecycleHeadStore(self.publications, client)
        self.coordinator = DynamoProjectionCoordinator(
            self.publications, self.leases, self.head_store, client
        )
        self.attempt_store = DynamoTableAttemptStore(self.publications, client)
        self.reader = Stage1WriterAuthorityReader(self.publications, self.leases, client)

    def instant(self) -> str:
        return self.clock.instant()

    def start_envelope(self, tag: str) -> str:
        """Render a fixed probe start envelope; the Athena probes compose real ones."""
        return f'{{"WorkGroup":"integration-probe-workgroup","Statement":"{tag}"}}'

    def seed_current(self, partition: str) -> PlainItem:
        """Seed one servable ``CURRENT`` pointer the finalizers may advance."""
        item: PlainItem = {
            "partition_key": partition,
            "record_key": "CURRENT",
            "publication_id": "integration-probe-pub-0009",
            "fencing_token": 9,
            "projected_publication_id": "integration-probe-pub-0003",
            "projected_fencing_token": 3,
            "projected_at": "2026-01-01T00:00:00+00:00",
        }
        self.dynamo.put_probe_item(self.publications, item)
        return item


def _unit_entry(unit_key: str, ordinal: int) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=unit_key,
        table_kind="current",
        merge_count=1,
        objects=(
            CanonicalObjectEntry(
                kind="manifest",
                reference=f"s3://integration-probe-authority/{unit_key}",
                content_checksum=CHECKSUM,
                publication_id=f"integration-probe-pub-{ordinal:04d}",
                partition_key=None,
                fencing_token=ordinal,
            ),
        ),
    )


def _required[T](value: T | None, what: str) -> T:
    if value is None:
        raise AssertionError(f"{what} is absent in the live store.")
    return value


def _evidence() -> SanitizedStartEvidence:
    return SanitizedStartEvidence(200, ProviderErrorClass.NONE)


# --- Plan world -----------------------------------------------------------------------------------


class PlanWorld:
    """One probe plan: submit → claim → persist → admit, all read back from real bytes."""

    def __init__(  # noqa: PLR0913 -- one probe builder; the replacement source is optional
        self,
        ctx: ProbeContext,
        tag: str,
        unit_tags: tuple[str, ...],
        *,
        source: PlanWorld | None = None,
    ) -> None:
        """Build one plan world; ``source`` makes this the planned-plan replacement of that plan.

        A replacement is the second intent of the same plan digest submitted while the source
        plan is owner-active: its root is the source plan key, its claim reserves generation
        ``source.generation + 1``, and its persistence carries the complete source evidence
        plus the compound two-event proof.
        """
        self.ctx = ctx
        self.tag = tag
        self.source = source
        self.units = tuple(
            _unit_entry(ctx.identity.table(unit_tag), index + 1)
            for index, unit_tag in enumerate(unit_tags)
        )
        origin_member = source_delivery_identity((ctx.identity.manifest(tag),))
        self.plan_digest = plan_digest_for_source_delivery(origin_member)
        self.origin = IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, origin_member)
        probe = self.submit_request()
        self.intent_id = probe.intent_id()
        self.roster_digest = probe.roster_digest()
        self.flows = {
            unit.unit_key: derive_unit_identity(
                UnitIdentityRequest(self.plan_digest, unit.unit_key, "integration-probe-bootstrap")
            ).flow_id
            for unit in self.units
        }
        self.generation = 0 if source is None else source.generation + 1
        self.internal_execution_id = internal_execution_identity(self.intent_id, self.generation)
        self.plan_key = plan_partition_key(self.plan_digest, self.internal_execution_id)
        self.plan_flow_id = derive_plan_identity(
            PlanIdentityRequest(self.plan_digest, self.internal_execution_id)
        ).flow_id

    # -- readback ----------------------------------------------------------------------------------

    def owner(self) -> SuccessorOwner:
        return _required(self.ctx.reader.owner(self.plan_digest), "owner")

    def intent(self) -> IntentMeta:
        return _required(self.ctx.reader.intent(self.plan_digest, self.intent_id), "intent")

    def meta(self) -> PlanMeta:
        return _required(self.ctx.reader.plan_meta(self.plan_key), "plan META")

    def meta_item(self) -> dict[str, object]:
        return _required(self.ctx.reader.plan_meta_item(self.plan_key), "plan META item")

    def unit_item(self, unit_key: str) -> dict[str, object]:
        return _required(self.ctx.reader.unit_item(self.plan_key, unit_key), "unit item")

    def plan_head(self) -> Stage1LifecycleHeadTuple:
        return _required(self.ctx.reader.head(self.plan_flow_id), "plan head")

    def plan_head_observation(self) -> PlanHeadObservation:
        return PlanHeadObservation(f"STATUS_FLOW#{self.plan_flow_id}", "HEAD", self.plan_head())

    # -- proofs ------------------------------------------------------------------------------------

    def expectations(self) -> PlanRequiredChildExpectations:
        members = tuple(
            sorted(
                (StatusChildExpectation(flow, self.generation) for flow in self.flows.values()),
                key=lambda member: member.child_flow_id.encode("utf-8"),
            )
        )
        return PlanRequiredChildExpectations(self.plan_flow_id, members)

    def persist_proof(self) -> Stage1LifecycleProof:
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PlanIdentityRequest(self.plan_digest, self.internal_execution_id),
                internal_key=(self.plan_key,),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                    LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_plan_recorded", FlowState.QUEUED, None, None
                ),
                stage="projection_plan_recorded",
                authority_at=self.ctx.clock.moment(),
                prior_head=None,
                required_child_expectations=self.expectations(),
            )
        )

    def later_plan_proof(  # noqa: PLR0913, PLR0917 -- one private proof builder
        self,
        transition_id: str,
        state: FlowState,
        outcome: CompletionOutcome | None,
        stage: str,
    ) -> Stage1LifecycleProof:
        head = self.plan_head()
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PlanIdentityRequest(self.plan_digest, self.internal_execution_id),
                internal_key=(self.plan_key,),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.LATER_PLAN_EVENT,
                    PriorOwnerEventSource(
                        head.parent_flow_id,
                        head.latest_event_id,
                        stage1_head_authority_document(head),
                    ),
                ),
                transition=Stage1LifecycleTransition(transition_id, state, outcome, None),
                stage=stage,
                authority_at=self.ctx.clock.moment(),
                prior_head=head,
                clock_started_at=self.ctx.reader.flow_root_clock(head.flow_id),
                required_child_expectations=self.expectations(),
            )
        )

    # -- request builders (each reads the live authority it conditions) ----------------------------

    def submit_request(self) -> SubmitPlanIntentRequest:
        owner = self.ctx.reader.owner(self.plan_digest)
        return SubmitPlanIntentRequest(
            plan_digest=self.plan_digest,
            root_plan_key=None if owner is None else owner.root_plan_key,
            source_plan_key=None,
            origin=self.origin,
            units=self.units,
            operation_instant=self.ctx.instant(),
            owner=owner,
        )

    def claim_request(self) -> ClaimPlanIntentRequest:
        source = self.source
        return ClaimPlanIntentRequest(
            plan_digest=self.plan_digest,
            intent=self.intent(),
            owner=self.owner(),
            operation_instant=self.ctx.instant(),
            active_plan=(
                None if source is None else ActivePlanObservation(source.plan_key, source.meta())
            ),
            plan_head=None if source is None else source.plan_head_observation(),
        )

    def persist_request(self) -> PersistPlanRequest:
        source = self.source
        return PersistPlanRequest(
            plan_digest=self.plan_digest,
            intent=self.intent(),
            units=self.units,
            owner=self.owner(),
            unit_flows=tuple(UnitFlowBinding(key, flow) for key, flow in self.flows.items()),
            proof=self.persist_proof() if source is None else self.replacement_proof(source),
            operation_instant=self.ctx.instant(),
            source_plan=None if source is None else source.replacement_evidence(),
        )

    def replacement_proof(self, source: PlanWorld) -> Stage1LifecycleProof:
        """Derive the sole compound ``N→N+2`` proof: source terminal then target recorded."""
        return compound_replacement_proof(
            Stage1CompoundReplacementRequest(
                plan_digest=self.plan_digest,
                prior_head=source.plan_head(),
                source_internal_key=(source.plan_key,),
                target_internal_key=(self.plan_key,),
                successor_internal_delivery_execution_id=self.internal_execution_id,
                terminal_transition=Stage1LifecycleTransition(
                    "semantic_plan_terminal",
                    FlowState.SUPERSEDED,
                    CompletionOutcome.SUPERSEDED,
                    None,
                ),
                successor_transition=Stage1LifecycleTransition(
                    "semantic_plan_recorded", FlowState.QUEUED, None, None
                ),
                source_expectations=source.expectations(),
                target_expectations=self.expectations(),
                authority_at=self.ctx.clock.moment(),
                clock_started_at=self.ctx.reader.flow_root_clock(source.plan_head().flow_id),
            )
        )

    def settled_references(self) -> tuple[SettledAttemptReference, ...]:
        """Read back every settled attempt reference through the production reader."""
        return self.ctx.reader.settled_references(self.plan_key)

    def replacement_evidence(self) -> SourcePlanReplacementEvidence:
        """Strongly read the complete source evidence through the production reader."""
        return self.ctx.reader.replacement_evidence(self.plan_key)

    def admit_request(self, unit_key: str) -> AdmitUnitRequest:
        meta = self.meta()
        item = self.unit_item(unit_key)
        unit = PlanUnit(
            unit=unit_entry_from_item(item),
            flow_id=str(item["flow_id"]),
            owner_generation=self.generation,
            status=PlanUnitStatus.QUEUED,
            admission=PlanUnitAdmission.PENDING,
            admission_deduplication_digest=None,
            admitted_at=None,
            created_at=str(item["created_at"]),
        )
        final = meta.admitted_count + 1 == meta.unit_count
        return AdmitUnitRequest(
            plan_digest=self.plan_digest,
            plan_key=self.plan_key,
            intent=self.intent(),
            owner=self.owner(),
            plan_meta=meta,
            unit=unit,
            deduplication_digest=canonical_authority_digest(
                ["integration-probe-admission", self.plan_key, unit_key]
            ),
            operation_instant=self.ctx.instant(),
            proof=(
                self.later_plan_proof(
                    "semantic_plan_admitted", FlowState.QUEUED, None, "projection_plan_admitted"
                )
                if final
                else None
            ),
        )

    def complete_plan_request(self) -> CompletePlanRequest:
        meta = self.meta()
        units: list[AttestedUnitObservation] = []
        attempts: list[SettledAttemptObservation] = []
        for item in self.ctx.reader.plan_unit_items(self.plan_key):
            unit_key = str(item.get("unit_key", ""))
            units.append(
                AttestedUnitObservation(
                    unit_key, str(item.get("flow_id", "")), meta.generation, item
                )
            )
            raw = item.get("attempt_references")
            if not isinstance(raw, list):
                continue
            for attempt_id in cast("list[object]", raw):
                if not isinstance(attempt_id, str):
                    continue
                attempt_item = self.ctx.reader.attempt_item(unit_key, attempt_id)
                if attempt_item is None or attempt_item.get("athena_usage_state") != "settled":
                    continue
                attempts.append(
                    SettledAttemptObservation(
                        unit_key,
                        attempt_id,
                        str(attempt_item.get("state", "")),
                        str(attempt_item.get("usage_authority_digest", "")),
                        attempt_item,
                    )
                )
        return CompletePlanRequest(
            plan_digest=self.plan_digest,
            plan_key=self.plan_key,
            owner=self.owner(),
            plan_meta=meta,
            units=tuple(units),
            attempts=tuple(attempts),
            proof=self.later_plan_proof(
                "semantic_plan_terminal",
                FlowState.COMPLETED,
                CompletionOutcome.SUCCEEDED,
                "projection_plan_terminal",
            ),
            operation_instant=self.ctx.instant(),
        )

    # -- convenience drivers -----------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Submit, claim, persist, and fully admit this plan through the live coordinator."""
        coordinator = self.ctx.coordinator
        coordinator.submit_plan_intent(self.submit_request())
        coordinator.claim_plan_intent(self.claim_request())
        coordinator.persist_plan(self.persist_request())
        for unit in self.units:
            coordinator.admit_unit(self.admit_request(unit.unit_key))

    def unit(self, unit_key: str, writer_tag: str) -> UnitWorld:
        return UnitWorld(self, unit_key, writer_tag)


# --- Unit world -----------------------------------------------------------------------------------


class UnitWorld:
    """One queued unit of a plan under one writer execution (one receive)."""

    def __init__(self, plan: PlanWorld, unit_key: str, writer_tag: str) -> None:
        self.plan = plan
        self.ctx = plan.ctx
        self.table_id = unit_key
        self.writer_execution_id = plan.ctx.identity.writer_execution(writer_tag)
        self.holder = queued_lease_holder(plan.plan_key, unit_key, self.writer_execution_id)
        identity = derive_unit_identity(
            UnitIdentityRequest(plan.plan_digest, unit_key, self.writer_execution_id)
        )
        self.flow_id = identity.flow_id
        self.execution_id = identity.execution_id

    # -- readback ----------------------------------------------------------------------------------

    def unit_item(self) -> dict[str, object]:
        return self.plan.unit_item(self.table_id)

    def lease(self) -> TableLeaseRecord:
        return _required(self.ctx.reader.lease(self.table_id), "lease")

    def fence(self) -> FenceObservation | None:
        return self.ctx.reader.fence(self.table_id)

    def attempt(self, attempt_id: str) -> AttemptObservation:
        return _required(self.ctx.reader.attempt(self.table_id, attempt_id), "attempt")

    def head(self) -> Stage1LifecycleHeadTuple:
        return _required(self.ctx.reader.head(self.flow_id), "unit head")

    def owner_snapshot(self) -> QueuedUnitAttemptOwner:
        return QueuedUnitAttemptOwner(self.plan.plan_key, self.table_id, self.unit_item())

    # -- proofs ------------------------------------------------------------------------------------

    def _identity_request(self) -> UnitIdentityRequest:
        return UnitIdentityRequest(self.plan.plan_digest, self.table_id, self.writer_execution_id)

    def claim_proof(self) -> Stage1LifecycleProof:
        head = self.ctx.reader.head(self.flow_id)
        plan_head = self.plan.plan_head()
        if head is None:
            topology = Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_UNIT_STARTED,
                PlanAdmissionSource(
                    plan_flow_id=plan_head.flow_id,
                    admitted_event_id=plan_head.latest_event_id,
                    plan_meta_authority=dict(self.plan.meta_item()),
                    complete_plan_head_authority=stage1_head_authority_document(plan_head),
                    admitted_event_authority=[
                        plan_head.latest_event_id,
                        plan_head.latest_authority_digest,
                    ],
                ),
            )
        else:
            topology = Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_UNIT_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
                ),
            )
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=self._identity_request(),
                internal_key=(self.plan.plan_digest, self.table_id),
                topology=topology,
                transition=Stage1LifecycleTransition(
                    "semantic_unit_started", FlowState.RUNNING, None, None
                ),
                stage="projection_unit_claimed",
                authority_at=self.ctx.clock.moment(),
                prior_head=head,
                clock_started_at=(
                    None if head is None else self.ctx.reader.flow_root_clock(head.flow_id)
                ),
                owner_generation=self.plan.generation,
            )
        )

    def event_proof(  # noqa: PLR0913, PLR0917 -- one private proof builder
        self,
        transition_id: str,
        state: FlowState,
        outcome: CompletionOutcome | None,
        typed_reason: str | None,
    ) -> Stage1LifecycleProof:
        head = self.head()
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=self._identity_request(),
                internal_key=(self.plan.plan_digest, self.table_id),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.LATER_UNIT_EVENT,
                    PriorOwnerEventSource(
                        head.parent_flow_id,
                        head.latest_event_id,
                        stage1_head_authority_document(head),
                    ),
                ),
                transition=Stage1LifecycleTransition(transition_id, state, outcome, typed_reason),
                stage="projection_unit_disposition",
                authority_at=self.ctx.clock.moment(),
                prior_head=head,
                clock_started_at=self.ctx.reader.flow_root_clock(head.flow_id),
                owner_generation=self.plan.generation,
            )
        )

    def pointer_proof(  # noqa: PLR0913, PLR0917 -- one private proof builder
        self, partition: str, publication_id: str, token: int, attested: Stage1LifecycleProof
    ) -> Stage1LifecycleProof:
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PointerIdentityRequest(
                    partition,
                    publication_id,
                    token,
                    PointerExecutionSource(
                        PointerExecutionSourceKind.QUEUED_UNIT_WRITER_EXECUTION,
                        self.writer_execution_id,
                    ),
                ),
                internal_key=(partition, publication_id, token),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.QUEUED_NONZERO_POINTER,
                    SameTransactionTerminalSource(attested.flow_id, attested.event_id),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_projection_servable",
                    FlowState.COMPLETED,
                    CompletionOutcome.SUCCEEDED,
                    None,
                ),
                stage="projection_servable",
                authority_at=self.ctx.clock.moment(),
                prior_head=None,
                publication_partition_key=partition,
            )
        )

    # -- request builders --------------------------------------------------------------------------

    def claim_request(
        self,
        *,
        prior_writer_execution_id: str | None = None,
        operation_instant: str | None = None,
    ) -> ClaimUnitRequest:
        return ClaimUnitRequest(
            plan_digest=self.plan.plan_digest,
            plan_key=self.plan.plan_key,
            physical_table_id=self.table_id,
            intent=self.plan.intent(),
            owner=self.plan.owner(),
            plan_meta=self.plan.meta(),
            unit_item=self.unit_item(),
            writer_execution_id=self.writer_execution_id,
            operation_instant=operation_instant or self.ctx.instant(),
            plan_head=self.plan.plan_head_observation(),
            proof=self.claim_proof(),
            prior_writer_execution_id=prior_writer_execution_id,
            prior_lease_holder=(
                None
                if prior_writer_execution_id is None
                else queued_lease_holder(
                    self.plan.plan_key, self.table_id, prior_writer_execution_id
                )
            ),
        )

    def acquire_request(
        self, *, expires_at: str | None = None, operation_instant: str | None = None
    ) -> AcquireTableFenceRequest:
        return AcquireTableFenceRequest(
            physical_table_id=self.table_id,
            holder=self.holder,
            operation_instant=operation_instant or self.ctx.instant(),
            new_expires_at=expires_at or self.ctx.clock.lease_expiry(900),
            observed_lease=self.ctx.reader.lease(self.table_id),
            observed_fence=self.fence(),
        )

    def begin_request(self, start_envelope: str) -> BeginTableAttemptRequest:
        return BeginTableAttemptRequest(
            physical_table_id=self.table_id,
            lease=self.lease(),
            operation_instant=self.ctx.instant(),
            observed_fence=self.fence(),
            owner=BeginQueuedUnitOwner(self.owner_snapshot(), self.plan.meta_item()),
            writer_execution_id=self.writer_execution_id,
            statement_kind=_STATEMENT_KIND,
            start_envelope=start_envelope,
        )

    def record_request(
        self, begun: BeginTableAttemptResult, query_execution_id: str
    ) -> RecordSubmittedAttemptRequest:
        return RecordSubmittedAttemptRequest(
            physical_table_id=self.table_id,
            attempt_id=begun.attempt_id,
            envelope_digest=begun.envelope_digest,
            client_request_token=begun.client_request_token,
            query_execution_id=query_execution_id,
            submitted_at=self.ctx.instant(),
            sanitized_start_evidence=_evidence(),
        )

    def complete_request(  # noqa: PLR0913 -- one private request builder
        self,
        attempt_id: str,
        state: ProviderTerminalState = ProviderTerminalState.SUCCEEDED,
        *,
        scanned_bytes: int | None = 1024,
        engine_ms: int | None = 250,
    ) -> CompleteTableAttemptRequest:
        return CompleteTableAttemptRequest(
            physical_table_id=self.table_id,
            fence=_required(self.fence(), "fence"),
            attempt=self.attempt(attempt_id),
            provider_state=state,
            terminal_at=self.ctx.instant(),
            owner=self.owner_snapshot(),
            query_count=1,
            scanned_bytes=scanned_bytes,
            engine_ms=engine_ms,
        )

    def release_request(self) -> ReleaseTableFenceRequest:
        return ReleaseTableFenceRequest(
            physical_table_id=self.table_id, lease=self.lease(), observed_fence=self.fence()
        )

    def refusal_request(self, reason: Stage1RefusalReason) -> RecordStage1RefusalRequest:
        return RecordStage1RefusalRequest(
            tag=Stage1RefusalTag.UNIT,
            reason=reason,
            refusal_instant=self.ctx.instant(),
            proof=self.event_proof(
                "semantic_unit_refused", FlowState.RETRYING, None, reason.value.lower()
            ),
            unit=RefusedUnitObservation(
                plan_digest=self.plan.plan_digest,
                plan_key=self.plan.plan_key,
                intent=self.plan.intent(),
                owner=self.plan.owner(),
                writer_execution_id=self.writer_execution_id,
                unit_item=self.unit_item(),
                meta_item=self.plan.meta_item(),
            ),
        )

    def settlement(self, disposition: OwnerDisposition) -> Stage1SettlementExpansion:
        """Expand this unit's unsettled provider-terminal attempts, as the writer does."""
        references = self.unit_item().get("attempt_references")
        attempt_ids = (
            [value for value in cast("list[object]", references) if isinstance(value, str)]
            if isinstance(references, list)
            else []
        )
        members: list[AthenaAttemptSettlement] = []
        for attempt_id in attempt_ids:
            observation = self.attempt(attempt_id)
            if observation.athena_usage_state != "unsettled":
                continue
            members.append(
                AthenaAttemptSettlement(
                    attempt=observation,
                    settlement=SettleStage1AthenaUsageRequest(
                        attempt_id=attempt_id,
                        flow_id=self.flow_id,
                        execution_id=self.execution_id,
                        evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[_UNIT_MEASUREMENT]),
                        owner_disposition=disposition,
                        observed_at=self.ctx.clock.moment(),
                        provider_terminal_state=StatusProviderTerminalState(
                            _required(observation.state, "attempt state")
                        ),
                        scanned_bytes=observation.scanned_bytes,
                        engine_ms=observation.engine_ms,
                    ),
                )
            )
        return Stage1SettlementExpansion(owner_disposition=disposition, attempts=tuple(members))

    def pointer_outcomes(
        self, partitions: tuple[str, ...], attested: Stage1LifecycleProof
    ) -> tuple[CurrentPointerOutcome, ...]:
        outcomes: list[CurrentPointerOutcome] = []
        for partition in partitions:
            current = _required(self.ctx.reader.current_item(partition), "CURRENT pointer")
            outcomes.append(
                CurrentPointerOutcome(
                    kind=PointerOutcomeKind.ADVANCED,
                    partition_identity=partition,
                    projected_publication_id="integration-probe-pub-0009",
                    projected_fencing_token=9,
                    proof=self.pointer_proof(partition, "integration-probe-pub-0009", 9, attested),
                    observed_current=current,
                )
            )
        return tuple(outcomes)

    def finalize_request(
        self, partitions: tuple[str, ...], prior_settled: tuple[SettledAttemptReference, ...] = ()
    ) -> FinalizeUnitRequest:
        attested = self.event_proof(
            "semantic_unit_attested", FlowState.COMPLETED, CompletionOutcome.SUCCEEDED, None
        )
        return FinalizeUnitRequest(
            plan_digest=self.plan.plan_digest,
            plan_key=self.plan.plan_key,
            physical_table_id=self.table_id,
            writer_execution_id=self.writer_execution_id,
            owner=self.plan.owner(),
            plan_meta=self.plan.meta(),
            unit_item=self.unit_item(),
            lease=self.lease(),
            observed_fence=self.fence(),
            pointers=self.pointer_outcomes(partitions, attested),
            settlement=self.settlement(OwnerDisposition.SUCCESSFUL),
            prior_settled_attempts=prior_settled,
            verified_checksums=(CHECKSUM,),
            operation_instant=self.ctx.instant(),
            proof=attested,
        )

    def fail_request(self, reason: UnitFailureReason) -> FailUnitRequest:
        return FailUnitRequest(
            plan_digest=self.plan.plan_digest,
            plan_key=self.plan.plan_key,
            physical_table_id=self.table_id,
            writer_execution_id=self.writer_execution_id,
            owner=self.plan.owner(),
            plan_meta=self.plan.meta(),
            lease=self.lease(),
            observed_fence=self.fence(),
            reason=reason,
            settlement=self.settlement(OwnerDisposition.RETRYABLE_FAILURE),
            prior_settled_attempts=(),
            operation_instant=self.ctx.instant(),
            proof=self.event_proof("semantic_unit_failed", FlowState.RETRYING, None, reason.value),
            unit_item=self.unit_item(),
        )

    def budget_fail_request(self) -> FailUnitRequest:
        reason = UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED
        return FailUnitRequest(
            plan_digest=self.plan.plan_digest,
            plan_key=self.plan.plan_key,
            physical_table_id=self.table_id,
            writer_execution_id=self.writer_execution_id,
            owner=self.plan.owner(),
            plan_meta=self.plan.meta(),
            lease=self.lease(),
            observed_fence=self.fence(),
            reason=reason,
            settlement=self.settlement(OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE),
            prior_settled_attempts=(),
            operation_instant=self.ctx.instant(),
            proof=self.event_proof("semantic_unit_failed", FlowState.RETRYING, None, reason.value),
            unit_item=None,
            budget_exhaustion=AttemptBudgetExhaustion(
                successor_units=self.plan.units,
                permanent_reservation_count=32,
                rejected_ordinal=33,
            ),
        )

    # -- convenience drivers -----------------------------------------------------------------------

    def run_attempt(self, tag: str) -> str:
        """Begin, record a synthetic submission, and complete one attempt; return its id."""
        coordinator = self.ctx.coordinator
        begun = coordinator.begin_table_attempt(self.begin_request(self.ctx.start_envelope(tag)))
        self.ctx.attempt_store.record_submitted_attempt(
            self.record_request(begun, self.ctx.identity.query_execution_id(tag))
        )
        coordinator.complete_table_attempt(self.complete_request(begun.attempt_id))
        return begun.attempt_id


# --- Inline world ---------------------------------------------------------------------------------


class InlineWorld:
    """One operator inline run against a probe table."""

    def __init__(self, ctx: ProbeContext, tag: str) -> None:
        self.ctx = ctx
        self.table_id = ctx.identity.table(tag)
        self.writer_kind = WriterKind.INGEST_DELIVERY_OPERATOR
        self.operation_id = ctx.identity.operation_id(tag)
        self.execution_id = ctx.identity.execution_id(tag)
        self.run_request = RunIdentityRequest(
            self.writer_kind, self.operation_id, self.execution_id, self.table_id
        )
        self.run_id = run_identity(self.run_request)
        self.holder = inline_lease_holder(self.run_request)
        identity = derive_inline_identity(
            InlineIdentityRequest(
                self.writer_kind.value, self.operation_id, self.table_id, self.execution_id
            )
        )
        self.flow_id = identity.flow_id

    # -- readback ----------------------------------------------------------------------------------

    def run_item(self) -> dict[str, object]:
        return _required(self.ctx.reader.run_item(self.table_id, self.run_id), "run item")

    def owner_snapshot(self) -> InlineRunAttemptOwner:
        return InlineRunAttemptOwner(self.table_id, self.run_id, self.run_item())

    def lease(self) -> TableLeaseRecord:
        return _required(self.ctx.reader.lease(self.table_id), "lease")

    def fence(self) -> FenceObservation | None:
        return self.ctx.reader.fence(self.table_id)

    def attempt(self, attempt_id: str) -> AttemptObservation:
        return _required(self.ctx.reader.attempt(self.table_id, attempt_id), "attempt")

    def head(self) -> Stage1LifecycleHeadTuple:
        return _required(self.ctx.reader.head(self.flow_id), "run head")

    # -- proofs ------------------------------------------------------------------------------------

    def _identity(self) -> InlineIdentityRequest:
        return InlineIdentityRequest(
            self.writer_kind.value, self.operation_id, self.table_id, self.execution_id
        )

    def recorded_proof(self) -> Stage1LifecycleProof:
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=self._identity(),
                internal_key=(
                    self.writer_kind.value,
                    self.operation_id,
                    self.table_id,
                    self.execution_id,
                ),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
                    LegitimateRootSource(RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_inline_run_recorded", FlowState.RUNNING, None, None
                ),
                stage="inline_run_recorded",
                authority_at=self.ctx.clock.moment(),
                prior_head=None,
            )
        )

    def later_proof(  # noqa: PLR0913, PLR0917 -- one private proof builder
        self,
        transition_id: str,
        state: FlowState,
        outcome: CompletionOutcome | None,
        typed_reason: str | None,
        stage: str,
    ) -> Stage1LifecycleProof:
        head = self.head()
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=self._identity(),
                internal_key=(
                    self.writer_kind.value,
                    self.operation_id,
                    self.table_id,
                    self.execution_id,
                ),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.LATER_INLINE_EVENT,
                    PriorOwnerEventSource(
                        head.parent_flow_id,
                        head.latest_event_id,
                        stage1_head_authority_document(head),
                    ),
                ),
                transition=Stage1LifecycleTransition(transition_id, state, outcome, typed_reason),
                stage=stage,
                authority_at=self.ctx.clock.moment(),
                prior_head=head,
                clock_started_at=self.ctx.reader.flow_root_clock(head.flow_id),
            )
        )

    def pointer_proof(  # noqa: PLR0913, PLR0917 -- one private proof builder
        self, partition: str, publication_id: str, token: int, terminal: Stage1LifecycleProof
    ) -> Stage1LifecycleProof:
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PointerIdentityRequest(
                    partition,
                    publication_id,
                    token,
                    PointerExecutionSource(
                        PointerExecutionSourceKind.INLINE_RUN_EXECUTION, self.execution_id
                    ),
                ),
                internal_key=(partition, publication_id, token),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.INLINE_NONZERO_POINTER,
                    SameTransactionTerminalSource(terminal.flow_id, terminal.event_id),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_projection_servable",
                    FlowState.COMPLETED,
                    CompletionOutcome.SUCCEEDED,
                    None,
                ),
                stage="projection_servable",
                authority_at=self.ctx.clock.moment(),
                prior_head=None,
                publication_partition_key=partition,
            )
        )

    # -- request builders --------------------------------------------------------------------------

    def persist_request(self) -> PersistInlineRunRequest:
        return PersistInlineRunRequest(
            writer_kind=self.writer_kind,
            operation_id=self.operation_id,
            execution_id=self.execution_id,
            physical_table_id=self.table_id,
            table_kind="current",
            statement_intent_kinds=("merge", "verify"),
            pointer_finalization_payload=None,
            created_at=self.ctx.instant(),
            proof=self.recorded_proof(),
        )

    def acquire_request(self, *, expires_at: str | None = None) -> AcquireTableFenceRequest:
        return AcquireTableFenceRequest(
            physical_table_id=self.table_id,
            holder=self.holder,
            operation_instant=self.ctx.instant(),
            new_expires_at=expires_at or self.ctx.clock.lease_expiry(900),
            observed_lease=self.ctx.reader.lease(self.table_id),
            observed_fence=self.fence(),
        )

    def begin_request(self, start_envelope: str) -> BeginTableAttemptRequest:
        return BeginTableAttemptRequest(
            physical_table_id=self.table_id,
            lease=self.lease(),
            operation_instant=self.ctx.instant(),
            observed_fence=self.fence(),
            owner=BeginInlineRunOwner(self.owner_snapshot()),
            writer_execution_id=self.execution_id,
            statement_kind=_STATEMENT_KIND,
            start_envelope=start_envelope,
        )

    def record_request(
        self, begun: BeginTableAttemptResult, query_execution_id: str
    ) -> RecordSubmittedAttemptRequest:
        return RecordSubmittedAttemptRequest(
            physical_table_id=self.table_id,
            attempt_id=begun.attempt_id,
            envelope_digest=begun.envelope_digest,
            client_request_token=begun.client_request_token,
            query_execution_id=query_execution_id,
            submitted_at=self.ctx.instant(),
            sanitized_start_evidence=_evidence(),
        )

    def complete_request(
        self,
        attempt_id: str,
        state: ProviderTerminalState = ProviderTerminalState.SUCCEEDED,
    ) -> CompleteTableAttemptRequest:
        return CompleteTableAttemptRequest(
            physical_table_id=self.table_id,
            fence=_required(self.fence(), "fence"),
            attempt=self.attempt(attempt_id),
            provider_state=state,
            terminal_at=self.ctx.instant(),
            owner=self.owner_snapshot(),
            query_count=1,
            scanned_bytes=2048,
            engine_ms=500,
        )

    def release_request(self) -> ReleaseTableFenceRequest:
        return ReleaseTableFenceRequest(
            physical_table_id=self.table_id, lease=self.lease(), observed_fence=self.fence()
        )

    def refusal_request(self) -> RecordStage1RefusalRequest:
        return RecordStage1RefusalRequest(
            tag=Stage1RefusalTag.INLINE,
            reason=Stage1RefusalReason.TABLE_ATTEMPT_PENDING,
            refusal_instant=self.ctx.instant(),
            proof=self.later_proof(
                "semantic_inline_run_refused",
                FlowState.RETRYING,
                None,
                "table_attempt_pending",
                "inline_run_refused",
            ),
            inline=RefusedInlineRunObservation(self.owner_snapshot()),
        )

    def settlement(self, disposition: OwnerDisposition) -> Stage1SettlementExpansion:
        references = self.run_item().get("attempt_references")
        attempt_ids = (
            [value for value in cast("list[object]", references) if isinstance(value, str)]
            if isinstance(references, list)
            else []
        )
        identity = derive_inline_identity(self._identity())
        members: list[AthenaAttemptSettlement] = []
        for attempt_id in attempt_ids:
            observation = self.attempt(attempt_id)
            if observation.athena_usage_state != "unsettled":
                continue
            members.append(
                AthenaAttemptSettlement(
                    attempt=observation,
                    settlement=SettleStage1AthenaUsageRequest(
                        attempt_id=attempt_id,
                        flow_id=identity.flow_id,
                        execution_id=identity.execution_id,
                        evaluation_binding=EvaluationBinding(
                            MEASUREMENT_REFS["semantic_inline_table_run"]
                        ),
                        owner_disposition=disposition,
                        observed_at=self.ctx.clock.moment(),
                        provider_terminal_state=StatusProviderTerminalState(
                            _required(observation.state, "attempt state")
                        ),
                        scanned_bytes=observation.scanned_bytes,
                        engine_ms=observation.engine_ms,
                    ),
                )
            )
        return Stage1SettlementExpansion(owner_disposition=disposition, attempts=tuple(members))

    def finalize_request(self, partitions: tuple[str, ...]) -> FinalizeInlineRequest:
        terminal = self.later_proof(
            "semantic_inline_run_terminal",
            FlowState.COMPLETED,
            CompletionOutcome.SUCCEEDED,
            None,
            "inline_run_terminal",
        )
        pointers: list[CurrentPointerOutcome] = []
        for partition in partitions:
            current = _required(self.ctx.reader.current_item(partition), "CURRENT pointer")
            pointers.append(
                CurrentPointerOutcome(
                    kind=PointerOutcomeKind.ADVANCED,
                    partition_identity=partition,
                    projected_publication_id="integration-probe-pub-0009",
                    projected_fencing_token=9,
                    proof=self.pointer_proof(partition, "integration-probe-pub-0009", 9, terminal),
                    observed_current=current,
                )
            )
        return FinalizeInlineRequest(
            physical_table_id=self.table_id,
            run=self.owner_snapshot(),
            lease=self.lease(),
            observed_fence=self.fence(),
            pointers=tuple(pointers),
            settlement=self.settlement(OwnerDisposition.SUCCESSFUL),
            operation_instant=self.ctx.instant(),
            proof=terminal,
        )

    # -- convenience drivers -----------------------------------------------------------------------

    def run_attempt(self, tag: str) -> str:
        coordinator = self.ctx.coordinator
        begun = coordinator.begin_table_attempt(self.begin_request(self.ctx.start_envelope(tag)))
        self.ctx.attempt_store.record_submitted_attempt(
            self.record_request(begun, self.ctx.identity.query_execution_id(tag))
        )
        coordinator.complete_table_attempt(self.complete_request(begun.attempt_id))
        return begun.attempt_id
