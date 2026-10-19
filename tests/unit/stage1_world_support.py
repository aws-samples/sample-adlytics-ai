# SPDX-License-Identifier: MIT-0
"""In-process Stage-1 world for the reconciler and recovery job unit tests.

``FakeDynamo`` naively applies ``TransactWriteItems``/``UpdateItem``/``PutItem`` and answers
``GetItem`` and the two sparse-index ``Query`` shapes from its item map; it proves nothing about
the real service (Slice A's live probes do that) and exists so the jobs' decision logic can be
exercised against the real coordinator, reader, and discovery adapter with no AWS. ``World`` drives
the real coordinator through the same plan → unit → attempt sequence the table writer performs.
Every identity is fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.stage1_discovery import DynamoStage1Discovery
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import (
    Stage1WriterAuthorityReader,
    unit_entry_from_item,
)
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from domain.operations.stage1_lifecycle_head import (
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    LegitimateRootSource,
    PlanAdmissionSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PriorOwnerEventSource,
    RequiredChildExpectation,
    RootDiscriminator,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_plan_identity,
    derive_unit_identity,
)
from domain.operations.status import FlowState
from domain.operations.table_attempt import (
    ProviderErrorClass,
    RunIdentityRequest,
    SanitizedStartEvidence,
    WriterKind,
    run_identity,
)
from domain.operations.table_lease import inline_lease_holder, queued_lease_holder
from domain.operations.write_plan import (
    CanonicalObjectEntry,
    CanonicalUnitEntry,
    IntentOrigin,
    IntentOriginKind,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    internal_execution_identity,
    plan_digest_for_source_delivery,
    plan_partition_key,
    source_delivery_identity,
)
from ports.outbound.projection_coordinator import (
    AcquireTableFenceRequest,
    AdmitUnitRequest,
    BeginInlineRunOwner,
    BeginQueuedUnitOwner,
    BeginTableAttemptRequest,
    BeginTableAttemptResult,
    ClaimPlanIntentRequest,
    ClaimUnitRequest,
    InlineRunAttemptOwner,
    PersistInlineRunRequest,
    PersistPlanRequest,
    PlanHeadObservation,
    QueuedUnitAttemptOwner,
    SubmitPlanIntentRequest,
    UnitFlowBinding,
)
from ports.outbound.table_attempt import RecordSubmittedAttemptRequest

PUBLICATIONS = "fictional-publications"
LEASES = "fictional-leases"
CHECKSUM = "0123456789abcdef" * 4
ENVELOPE = '{"Database":"fictional_db","QueryString":"SELECT 1","WorkGroup":"fictional-wg"}'
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _alias(names: dict[str, str], name: str) -> str:
    text = name.strip()
    return names.get(text, text)


@guard_reserved_words
@dataclass
class FakeDynamo:
    """A naive in-memory DynamoDB: no conditions are evaluated unless a failure is forced."""

    items: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str, str], dict[str, Any]]
    )
    transacts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fail_next_transactions: int = 0

    def get_item(self, **request: Any) -> dict[str, object]:
        table = cast("str", request["TableName"])
        key = cast("dict[str, dict[str, str]]", request["Key"])
        record = key.get("record_key", {}).get("S", "")
        item = self.items.get((table, key["partition_key"]["S"], record))
        return {} if item is None else {"Item": item}

    def put_item(self, **request: Any) -> dict[str, object]:
        item = cast("dict[str, Any]", request["Item"])
        record = cast("dict[str, dict[str, str]]", item).get("record_key", {}).get("S", "")
        self.items[(request["TableName"], item["partition_key"]["S"], record)] = dict(item)
        return {}

    def update_item(self, **request: Any) -> dict[str, object]:
        self._apply({"Update": dict(request)})
        return {}

    def query(self, **request: Any) -> dict[str, object]:
        table = cast("str", request["TableName"])
        values = cast("dict[str, dict[str, str]]", request["ExpressionAttributeValues"])
        condition = cast("str", request["KeyConditionExpression"])
        index = request.get("IndexName")
        matches: list[dict[str, Any]] = []
        for (item_table, _, _), item in self.items.items():
            if item_table != table:
                continue
            if index is None:
                partition_value = values.get(":plan", values.get(":flow", values.get(":partition")))
                prefix_value = values.get(":unit", values.get(":event", values.get(":prefix")))
                if partition_value is None or prefix_value is None:
                    raise AssertionError(f"unexpected key-condition values {sorted(values)}")
                partition = partition_value["S"]
                prefix = prefix_value["S"]
                if item["partition_key"]["S"] == partition and item.get("record_key", {}).get(
                    "S", ""
                ).startswith(prefix):
                    matches.append(item)
                continue
            hash_name, _, rest = condition.partition(" = :partition")
            hash_value = values[":partition"]["S"]
            if item.get(hash_name, {}).get("S") != hash_value:
                continue
            sort_name = "reconcile_after_or_block" if "reconcile" in rest else "ready_order"
            sort_value = item.get(sort_name, {}).get("S", "")
            if ":bound" in values and sort_value > values[":bound"]["S"]:
                continue
            if ":cursor" in values and sort_value <= values[":cursor"]["S"]:
                continue
            matches.append(item)
        matches.sort(
            key=lambda item: (
                item.get("reconcile_after_or_block", item.get("ready_order", {"S": ""}))["S"],
                item["partition_key"]["S"],
            )
        )
        limit = cast("int", request.get("Limit", len(matches)))
        return {"Items": matches[:limit]}

    def transact_write_items(self, **request: Any) -> dict[str, object]:
        self.transacts.append(request)
        if self.fail_next_transactions:
            self.fail_next_transactions -= 1
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException", "Message": "fixture"}},
                "TransactWriteItems",
            )
        for member in cast("list[dict[str, Any]]", request["TransactItems"]):
            self._apply(member)
        return {}

    def _apply(self, member: dict[str, Any]) -> None:
        if "Put" in member:
            body = cast("dict[str, Any]", member["Put"])
            item = cast("dict[str, Any]", body["Item"])
            record = cast("dict[str, dict[str, str]]", item).get("record_key", {}).get("S", "")
            self.items[(body["TableName"], item["partition_key"]["S"], record)] = dict(item)
        elif "Update" in member:
            body = cast("dict[str, Any]", member["Update"])
            key = cast("dict[str, dict[str, str]]", body["Key"])
            record = key.get("record_key", {}).get("S", "")
            address = (cast("str", body["TableName"]), key["partition_key"]["S"], record)
            stored = dict(self.items.get(address, dict(key)))
            names = cast("dict[str, str]", body.get("ExpressionAttributeNames", {}))
            values = cast("dict[str, Any]", body["ExpressionAttributeValues"])
            expression = cast("str", body["UpdateExpression"])
            if expression.startswith("ADD "):
                # S6: the DRAIN_SEQ counter, `ADD seq :one`.
                name, value = expression.removeprefix("ADD ").split()
                current = int(stored.get(name, {"N": "0"})["N"])
                stored[name] = {"N": str(current + int(values[value]["N"]))}
                self.items[address] = stored
                return
            set_part, _, remove_part = expression.partition(" REMOVE ")
            for assignment in set_part.removeprefix("SET ").split(", "):
                name, _, value = assignment.partition(" = ")
                stored[_alias(names, name)] = values[value.strip()]
            for name in remove_part.split(", ") if remove_part else ():
                stored.pop(_alias(names, name), None)
            self.items[address] = stored


class Clock:
    """Strictly increasing canonical instants one millisecond apart."""

    def __init__(self, start_micros: int = 1_790_000_000_000_000) -> None:
        self.micros = start_micros

    def instant(self) -> str:
        self.micros += 1_000
        return f"{self.micros:020d}"

    def moment(self) -> datetime:
        return _EPOCH + timedelta(microseconds=self.micros)

    def lease_expiry(self, seconds: int) -> str:
        return (self.moment().replace(microsecond=0) + timedelta(seconds=seconds)).isoformat()


def _unit_entry(unit_key: str, ordinal: int) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=unit_key,
        table_kind="current",
        merge_count=1,
        objects=(
            CanonicalObjectEntry(
                kind="manifest",
                reference=f"s3://fictional-authority/{unit_key}",
                content_checksum=CHECKSUM,
                publication_id=f"fictional-pub-{ordinal:04d}",
                partition_key=None,
                fencing_token=ordinal,
            ),
        ),
    )


def _required[T](value: T | None, what: str) -> T:
    if value is None:
        raise AssertionError(f"{what} is absent in the fake store.")
    return value


@dataclass
class World:
    """The real coordinator, reader, attempt store, and discovery over one fake DynamoDB."""

    dynamo: FakeDynamo = field(default_factory=FakeDynamo)
    clock: Clock = field(default_factory=Clock)

    def __post_init__(self) -> None:
        client = cast("Any", self.dynamo)
        self.head_store = DynamoStage1LifecycleHeadStore(PUBLICATIONS, client)
        self.coordinator = DynamoProjectionCoordinator(
            PUBLICATIONS, LEASES, self.head_store, client
        )
        self.reader = Stage1WriterAuthorityReader(PUBLICATIONS, LEASES, client)
        self.attempts = DynamoTableAttemptStore(PUBLICATIONS, client)
        self.discovery = DynamoStage1Discovery(PUBLICATIONS, client)

    def instant(self) -> str:
        return self.clock.instant()

    # -- plan ------------------------------------------------------------------------------------

    def submit_intent(self, tag: str, unit_keys: tuple[str, ...]) -> tuple[str, str]:
        """Submit one pending source-delivery intent; return ``(plan_digest, intent_id)``."""
        origin_member = source_delivery_identity(((f"fictional/{tag}.json", "v1"),))
        plan_digest = plan_digest_for_source_delivery(origin_member)
        owner = self.reader.owner(plan_digest)
        request = SubmitPlanIntentRequest(
            plan_digest=plan_digest,
            root_plan_key=None if owner is None else owner.root_plan_key,
            source_plan_key=None,
            origin=IntentOrigin(IntentOriginKind.SOURCE_DELIVERY, origin_member),
            units=tuple(_unit_entry(key, index + 1) for index, key in enumerate(unit_keys)),
            operation_instant=self.instant(),
            owner=owner,
        )
        self.coordinator.submit_plan_intent(request)
        return plan_digest, request.intent_id()

    def bootstrap_plan(self, tag: str, unit_keys: tuple[str, ...]) -> str:
        """Submit, claim, persist, and fully admit one plan; return its plan key."""
        plan_digest, intent_id = self.submit_intent(tag, unit_keys)
        intent = _required(self.reader.intent(plan_digest, intent_id), "intent")
        owner = _required(self.reader.owner(plan_digest), "owner")
        self.coordinator.claim_plan_intent(
            ClaimPlanIntentRequest(
                plan_digest, intent, owner, self.instant(), active_plan=None, plan_head=None
            )
        )
        intent = _required(self.reader.intent(plan_digest, intent_id), "intent")
        owner = _required(self.reader.owner(plan_digest), "owner")
        units = tuple(_unit_entry(key, index + 1) for index, key in enumerate(unit_keys))
        generation = 0
        execution = internal_execution_identity(intent_id, generation)
        plan_key = plan_partition_key(plan_digest, execution)
        flows = tuple(
            UnitFlowBinding(
                key, derive_unit_identity(UnitIdentityRequest(plan_digest, key, "fixture")).flow_id
            )
            for key in unit_keys
        )
        plan_flow = derive_plan_identity(PlanIdentityRequest(plan_digest, execution)).flow_id
        expectations = PlanRequiredChildExpectations(
            plan_flow,
            tuple(
                sorted(
                    (RequiredChildExpectation(binding.flow_id, generation) for binding in flows),
                    key=lambda member: member.child_flow_id.encode("utf-8"),
                )
            ),
        )
        proof = derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PlanIdentityRequest(plan_digest, execution),
                internal_key=(plan_key,),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                    LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_plan_recorded", FlowState.QUEUED, None, None
                ),
                stage="projection_plan_recorded",
                authority_at=self.clock.moment(),
                prior_head=None,
                required_child_expectations=expectations,
            )
        )
        self.coordinator.persist_plan(
            PersistPlanRequest(plan_digest, intent, units, owner, flows, proof, self.instant())
        )
        for key in unit_keys:
            self.admit(plan_digest, plan_key, key, expectations)
        return plan_key

    def admit(  # noqa: PLR0913, PLR0917 -- one fixture driver
        self,
        plan_digest: str,
        plan_key: str,
        unit_key: str,
        expectations: PlanRequiredChildExpectations,
    ) -> None:
        meta = _required(self.reader.plan_meta(plan_key), "META")
        item = _required(self.reader.unit_item(plan_key, unit_key), "unit")
        unit = PlanUnit(
            unit=unit_entry_from_item(item),
            flow_id=str(item["flow_id"]),
            owner_generation=meta.generation,
            status=PlanUnitStatus.QUEUED,
            admission=PlanUnitAdmission.PENDING,
            admission_deduplication_digest=None,
            admitted_at=None,
            created_at=str(item["created_at"]),
        )
        final = meta.admitted_count + 1 == meta.unit_count
        proof = None
        if final:
            head = _required(self.plan_head(plan_digest, meta.internal_execution_id), "head")
            proof = derive_stage1_lifecycle_proof(
                Stage1LifecycleProofRequest(
                    identity=PlanIdentityRequest(plan_digest, meta.internal_execution_id),
                    internal_key=(plan_key,),
                    topology=Stage1TopologyRequest(
                        Stage1OwnerTransition.LATER_PLAN_EVENT,
                        PriorOwnerEventSource(
                            head.parent_flow_id,
                            head.latest_event_id,
                            stage1_head_authority_document(head),
                        ),
                    ),
                    transition=Stage1LifecycleTransition(
                        "semantic_plan_admitted", FlowState.QUEUED, None, None
                    ),
                    stage="projection_plan_admitted",
                    authority_at=self.clock.moment(),
                    prior_head=head,
                    clock_started_at=self.reader.flow_root_clock(head.flow_id),
                    required_child_expectations=expectations,
                )
            )
        self.coordinator.admit_unit(
            AdmitUnitRequest(
                plan_digest=plan_digest,
                plan_key=plan_key,
                intent=_required(self.reader.intent(plan_digest, meta.origin_intent_id), "intent"),
                owner=_required(self.reader.owner(plan_digest), "owner"),
                plan_meta=meta,
                unit=unit,
                deduplication_digest="ab" * 32,
                operation_instant=self.instant(),
                proof=proof,
            )
        )

    def plan_head(self, plan_digest: str, execution: str) -> Any:
        return self.reader.head(
            derive_plan_identity(PlanIdentityRequest(plan_digest, execution)).flow_id
        )

    # -- queued unit -------------------------------------------------------------------------------

    def claim_and_lease_unit(self, plan_digest: str, plan_key: str, unit_key: str) -> str:
        """Claim one admitted unit and acquire its lease; return the writer execution id."""
        writer = "f" * 64
        meta = _required(self.reader.plan_meta(plan_key), "META")
        head = _required(self.plan_head(plan_digest, meta.internal_execution_id), "head")
        unit_flow = derive_unit_identity(UnitIdentityRequest(plan_digest, unit_key, writer)).flow_id
        proof = derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=UnitIdentityRequest(plan_digest, unit_key, writer),
                internal_key=(plan_digest, unit_key),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_UNIT_STARTED,
                    PlanAdmissionSource(
                        plan_flow_id=head.flow_id,
                        admitted_event_id=head.latest_event_id,
                        plan_meta_authority=dict(
                            _required(self.reader.plan_meta_item(plan_key), "META")
                        ),
                        complete_plan_head_authority=stage1_head_authority_document(head),
                        admitted_event_authority=[
                            head.latest_event_id,
                            head.latest_authority_digest,
                        ],
                    ),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_unit_started", FlowState.RUNNING, None, None
                ),
                stage="projection_unit_claimed",
                authority_at=self.clock.moment(),
                prior_head=self.reader.head(unit_flow),
                clock_started_at=(
                    None
                    if self.reader.head(unit_flow) is None
                    else self.reader.flow_root_clock(unit_flow)
                ),
                owner_generation=meta.generation,
            )
        )
        self.coordinator.claim_unit(
            ClaimUnitRequest(
                plan_digest=plan_digest,
                plan_key=plan_key,
                physical_table_id=unit_key,
                intent=_required(self.reader.intent(plan_digest, meta.origin_intent_id), "intent"),
                owner=_required(self.reader.owner(plan_digest), "owner"),
                plan_meta=meta,
                unit_item=_required(self.reader.unit_item(plan_key, unit_key), "unit"),
                writer_execution_id=writer,
                operation_instant=self.instant(),
                plan_head=PlanHeadObservation(f"STATUS_FLOW#{head.flow_id}", "HEAD", head),
                proof=proof,
            )
        )
        self.coordinator.acquire_table_fence(
            AcquireTableFenceRequest(
                physical_table_id=unit_key,
                holder=queued_lease_holder(plan_key, unit_key, writer),
                operation_instant=self.instant(),
                new_expires_at=self.clock.lease_expiry(900),
                observed_lease=self.reader.lease(unit_key),
                observed_fence=self.reader.fence(unit_key),
            )
        )
        return writer

    def begin_queued(self, plan_key: str, unit_key: str, writer: str) -> BeginTableAttemptResult:
        return self.coordinator.begin_table_attempt(
            BeginTableAttemptRequest(
                physical_table_id=unit_key,
                lease=_required(self.reader.lease(unit_key), "lease"),
                operation_instant=self.instant(),
                observed_fence=self.reader.fence(unit_key),
                owner=BeginQueuedUnitOwner(
                    QueuedUnitAttemptOwner(
                        plan_key,
                        unit_key,
                        _required(self.reader.unit_item(plan_key, unit_key), "u"),
                    ),
                    _required(self.reader.plan_meta_item(plan_key), "META"),
                ),
                writer_execution_id=writer,
                statement_kind="merge",
                start_envelope=ENVELOPE,
            )
        )

    def record(self, table_id: str, begun: BeginTableAttemptResult, query_id: str) -> None:
        self.attempts.record_submitted_attempt(
            RecordSubmittedAttemptRequest(
                physical_table_id=table_id,
                attempt_id=begun.attempt_id,
                envelope_digest=begun.envelope_digest,
                client_request_token=begun.client_request_token,
                query_execution_id=query_id,
                submitted_at=self.instant(),
                sanitized_start_evidence=SanitizedStartEvidence(200, ProviderErrorClass.NONE),
            )
        )

    # -- inline run --------------------------------------------------------------------------------

    def open_inline_run(self, table_id: str, tag: str) -> tuple[str, str]:
        """Persist an operator inline run and acquire its lease; return ``(run_id, execution)``."""
        writer_kind = WriterKind.INGEST_DELIVERY_OPERATOR
        operation_id = f"fictional-op-{tag}"
        execution_id = f"fictional-exec-{tag}"
        run_id = run_identity(RunIdentityRequest(writer_kind, operation_id, execution_id, table_id))
        proof = derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=InlineIdentityRequest(
                    writer_kind.value, operation_id, table_id, execution_id
                ),
                internal_key=(writer_kind.value, operation_id, table_id, execution_id),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
                    LegitimateRootSource(RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE),
                ),
                transition=Stage1LifecycleTransition(
                    "semantic_inline_run_recorded", FlowState.RUNNING, None, None
                ),
                stage="inline_run_recorded",
                authority_at=self.clock.moment(),
                prior_head=None,
            )
        )
        self.coordinator.persist_inline_run(
            PersistInlineRunRequest(
                writer_kind=writer_kind,
                operation_id=operation_id,
                execution_id=execution_id,
                physical_table_id=table_id,
                table_kind="current",
                statement_intent_kinds=("merge", "verify"),
                pointer_finalization_payload=None,
                created_at=self.instant(),
                proof=proof,
            )
        )
        self.coordinator.acquire_table_fence(
            AcquireTableFenceRequest(
                physical_table_id=table_id,
                holder=inline_lease_holder(
                    RunIdentityRequest(writer_kind, operation_id, execution_id, table_id)
                ),
                operation_instant=self.instant(),
                new_expires_at=self.clock.lease_expiry(900),
                observed_lease=self.reader.lease(table_id),
                observed_fence=self.reader.fence(table_id),
            )
        )
        return run_id, execution_id

    def begin_inline(
        self, table_id: str, run_id: str, execution_id: str
    ) -> BeginTableAttemptResult:
        return self.coordinator.begin_table_attempt(
            BeginTableAttemptRequest(
                physical_table_id=table_id,
                lease=_required(self.reader.lease(table_id), "lease"),
                operation_instant=self.instant(),
                observed_fence=self.reader.fence(table_id),
                owner=BeginInlineRunOwner(
                    InlineRunAttemptOwner(
                        table_id, run_id, _required(self.reader.run_item(table_id, run_id), "run")
                    )
                ),
                writer_execution_id=execution_id,
                statement_kind="merge",
                start_envelope=ENVELOPE,
            )
        )


__all__ = ["CHECKSUM", "ENVELOPE", "LEASES", "PUBLICATIONS", "Clock", "FakeDynamo", "World"]
