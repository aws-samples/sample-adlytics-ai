# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 lifecycle-head proofs, succession, compound replacement, and replay."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_operational_status import (
    _retained_event_request,  # pyright: ignore[reportPrivateUsage] -- reader half of the schema
)
from domain.operations.stage1_lifecycle_head import (
    Stage1CompoundReplacementRequest,
    Stage1LifecycleHeadInvariantError,
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    Stage1PersistedEventObservation,
    Stage1ReplayClassification,
    Stage1ReplayObservation,
    classify_stage1_replay,
    compound_replacement_proof,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    EventIdentityRequest,
    LegitimateRootSource,
    PlanAdmissionSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    RequiredChildExpectation,
    RootDiscriminator,
    SameTransactionTerminalSource,
    Stage1EventAuthorityRequest,
    Stage1OwnerTransition,
    Stage1Surface,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_event_id,
    event_authority_digest,
    reattest_stage1_event,
    stage1_authority_time,
)
from domain.operations.status import (
    CompletionOutcome,
    FlowState,
    Progress,
    ReductionKind,
    canonical_authority_digest,
    event_from_document,
    reduce_event,
)
from ports.outbound.stage1_lifecycle_head import Stage1LifecycleApplyKind

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

NOW = datetime(2026, 9, 19, 12, 34, 56, 789012, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)
PLAN_DIGEST = "plan-fixture"
PLAN_KEY = "WRITE_PLAN#fixture"
PLAN_FLOW = "68cb3192-c252-5fed-bbe8-8f0ab98f74b6"
PLAN_EXECUTION = "76db826e-6da1-57ba-b170-75d627f053a3"
UNIT_FLOW = "960cfeca-0bad-57fd-b4b0-383e15d59085"
UNIT_EXECUTION = "ef642f3b-65f1-580f-b01b-e2b7102b98f9"
POINTER_FLOW = "e4de2f19-58db-5f51-b924-b4e5118d4db3"
POINTER_EXECUTION = "6f03579d-6e36-5f49-a906-e69c979df18e"
INLINE_FLOW = "ac30cd31-fad5-5dcb-a821-fe91c54eafc0"
ADMITTED_EVENT_ID = "33333333-3333-4333-8333-333333333333"
PUBLICATION_PARTITION = "PUBLICATION#fictional.dataset|2026-09-19|whole"


def _plan_expectations(*, generation: int = 7) -> PlanRequiredChildExpectations:
    return PlanRequiredChildExpectations(
        PLAN_FLOW, (RequiredChildExpectation(UNIT_FLOW, generation),)
    )


def _plan_root_request() -> Stage1LifecycleProofRequest:
    return Stage1LifecycleProofRequest(
        identity=PlanIdentityRequest(PLAN_DIGEST, "delivery-execution-fixture"),
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
        required_child_expectations=_plan_expectations(),
    )


def _plan_successor_request(
    head: Stage1LifecycleHeadTuple,
    *,
    selector: str = "delivery-execution-fixture",
    authority_at: datetime = LATER,
) -> Stage1LifecycleProofRequest:
    return Stage1LifecycleProofRequest(
        identity=PlanIdentityRequest(PLAN_DIGEST, selector),
        internal_key=(PLAN_KEY,),
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
        authority_at=authority_at,
        prior_head=head,
        clock_started_at=NOW,
        required_child_expectations=_plan_expectations(),
    )


def _unit_root_request() -> Stage1LifecycleProofRequest:
    return Stage1LifecycleProofRequest(
        identity=UnitIdentityRequest(PLAN_DIGEST, "unit-fixture", "writer-execution-fixture"),
        internal_key=(PLAN_KEY, "unit-fixture"),
        topology=Stage1TopologyRequest(
            Stage1OwnerTransition.FIRST_UNIT_STARTED,
            PlanAdmissionSource(
                PLAN_FLOW,
                ADMITTED_EVENT_ID,
                {"meta": "exact"},
                {"head": "complete"},
                {"event": "admitted"},
            ),
        ),
        transition=Stage1LifecycleTransition(
            "semantic_unit_started", FlowState.RUNNING, None, None
        ),
        stage="projection_unit_claimed",
        authority_at=NOW,
        prior_head=None,
        progress=Progress(0, 1, "table_unit"),
        owner_generation=7,
    )


def _pointer_request() -> Stage1LifecycleProofRequest:
    return Stage1LifecycleProofRequest(
        identity=PointerIdentityRequest(
            "fictional.dataset|2026-09-19|whole",
            "publication-fixture",
            7,
            PointerExecutionSource(
                PointerExecutionSourceKind.INLINE_RUN_EXECUTION, "inline-execution-fixture"
            ),
        ),
        internal_key=("fictional.dataset|2026-09-19|whole", "publication-fixture", 7),
        topology=Stage1TopologyRequest(
            Stage1OwnerTransition.INLINE_NONZERO_POINTER,
            SameTransactionTerminalSource(INLINE_FLOW, ADMITTED_EVENT_ID),
        ),
        transition=Stage1LifecycleTransition(
            "semantic_projection_servable",
            FlowState.COMPLETED,
            CompletionOutcome.SUCCEEDED,
            None,
        ),
        stage="projection_servable",
        authority_at=NOW,
        prior_head=None,
        publication_partition_key=PUBLICATION_PARTITION,
    )


def _compound_request(head: Stage1LifecycleHeadTuple) -> Stage1CompoundReplacementRequest:
    return Stage1CompoundReplacementRequest(
        plan_digest=PLAN_DIGEST,
        prior_head=head,
        source_internal_key=(PLAN_KEY,),
        target_internal_key=("WRITE_PLAN#successor",),
        successor_internal_delivery_execution_id="delivery-execution-successor",
        terminal_transition=Stage1LifecycleTransition(
            "semantic_plan_terminal",
            FlowState.SUPERSEDED,
            CompletionOutcome.SUPERSEDED,
            None,
        ),
        successor_transition=Stage1LifecycleTransition(
            "semantic_plan_recorded", FlowState.QUEUED, None, None
        ),
        source_expectations=_plan_expectations(),
        target_expectations=_plan_expectations(generation=8),
        authority_at=LATER,
        clock_started_at=NOW,
    )


def _resulting_head(proof: Stage1LifecycleProof) -> Stage1LifecycleHeadTuple:
    head = proof.resulting_head
    assert head is not None
    return head


def _byte_identical_observation(
    proof: Stage1LifecycleProof, *, head: Stage1LifecycleHeadTuple | None = None
) -> Stage1ReplayObservation:
    return Stage1ReplayObservation(
        tuple(
            Stage1PersistedEventObservation(
                fragment.reattest, fragment.payload, fragment.outbox.immutable_payload_digest
            )
            for fragment in proof.events
        ),
        proof.resulting_head if head is None else head,
    )


class TestProofDerivation:
    def test_plan_root_proof_pins_keys_identity_and_the_complete_head(self) -> None:
        proof = derive_stage1_lifecycle_proof(_plan_root_request())

        expected_digest = event_authority_digest(
            Stage1EventAuthorityRequest(
                surface=Stage1Surface.PLAN,
                internal_key=(PLAN_KEY,),
                transition_id="semantic_plan_recorded",
                lifecycle_state=FlowState.QUEUED,
                completion_outcome=None,
                typed_reason=None,
                authority_time=stage1_authority_time(NOW),
            )
        )
        expected_event_id = derive_event_id(
            EventIdentityRequest(Stage1Surface.PLAN, expected_digest, 1, FlowState.QUEUED)
        )
        assert proof.partition_key == f"STATUS_FLOW#{PLAN_FLOW}"
        assert proof.head_record_key == "HEAD"
        assert (proof.flow_id, proof.execution_id) == (PLAN_FLOW, PLAN_EXECUTION)
        assert (proof.event_id, proof.authority_digest) == (expected_event_id, expected_digest)
        assert proof.events[0].record_key == f"EVENT#{expected_digest}"
        assert proof.prior_head is None
        assert proof.resulting_head == Stage1LifecycleHeadTuple(
            surface=Stage1Surface.PLAN,
            flow_id=PLAN_FLOW,
            current_execution_id=PLAN_EXECUTION,
            current_internal_execution_id="delivery-execution-fixture",
            predecessor_execution_id=None,
            latest_event_id=expected_event_id,
            latest_authority_digest=expected_digest,
            latest_transition_sequence=1,
            latest_state=FlowState.QUEUED,
            parent_flow_id=None,
            causation_event_id=None,
        )
        retained = proof.events[0].reattest.proof
        assert (retained.transition_sequence, "transition sequence") == (1, "transition sequence")
        assert retained.predecessor_event_id is None
        assert retained.predecessor_execution_id is None

    def test_head_tuple_carries_exactly_the_twelve_closed_fields(self) -> None:
        assert {member.name for member in fields(Stage1LifecycleHeadTuple)} == {
            "head_contract_version",
            "surface",
            "flow_id",
            "current_execution_id",
            "current_internal_execution_id",
            "predecessor_execution_id",
            "latest_event_id",
            "latest_authority_digest",
            "latest_transition_sequence",
            "latest_state",
            "parent_flow_id",
            "causation_event_id",
        }

    def test_same_internal_selector_reuses_the_persisted_public_execution(self) -> None:
        root = derive_stage1_lifecycle_proof(_plan_root_request())
        head = _resulting_head(root)

        successor = derive_stage1_lifecycle_proof(_plan_successor_request(head))

        retained = successor.events[0].reattest.proof
        assert successor.execution_id == PLAN_EXECUTION
        assert retained.predecessor_execution_id is None
        assert (retained.transition_sequence, "transition sequence") == (2, "transition sequence")
        assert retained.predecessor_event_id == head.latest_event_id
        next_head = _resulting_head(successor)
        assert next_head.current_execution_id == PLAN_EXECUTION
        assert next_head.predecessor_execution_id is None

    def test_new_internal_selector_derives_a_successor_execution_naming_the_predecessor(
        self,
    ) -> None:
        root = derive_stage1_lifecycle_proof(_plan_root_request())
        head = _resulting_head(root)

        retry = derive_stage1_lifecycle_proof(
            _plan_successor_request(head, selector="delivery-execution-retry")
        )

        retained = retry.events[0].reattest.proof
        assert retry.execution_id != PLAN_EXECUTION
        assert retained.predecessor_execution_id == PLAN_EXECUTION
        next_head = _resulting_head(retry)
        assert next_head.current_execution_id == retry.execution_id
        assert next_head.current_internal_execution_id == "delivery-execution-retry"
        assert next_head.predecessor_execution_id == PLAN_EXECUTION

    def test_unit_root_copies_plan_admission_topology_into_event_and_head(self) -> None:
        proof = derive_stage1_lifecycle_proof(_unit_root_request())

        retained = proof.events[0].reattest.proof
        assert (proof.flow_id, proof.execution_id) == (UNIT_FLOW, UNIT_EXECUTION)
        assert (retained.parent_flow_id, retained.causation_event_id) == (
            PLAN_FLOW,
            ADMITTED_EVENT_ID,
        )
        assert retained.predecessor_event_id is None
        head = _resulting_head(proof)
        assert (head.parent_flow_id, head.causation_event_id) == (PLAN_FLOW, ADMITTED_EVENT_ID)
        assert proof.events[0].reattest.owner_generation == 7

    def test_topology_disagreeing_with_the_complete_prior_head_fails_closed(self) -> None:
        head = _resulting_head(derive_stage1_lifecycle_proof(_plan_root_request()))
        request = _plan_successor_request(head)
        drifted = Stage1LifecycleProofRequest(
            identity=request.identity,
            internal_key=request.internal_key,
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_PLAN_EVENT,
                PriorOwnerEventSource(
                    head.parent_flow_id, ADMITTED_EVENT_ID, stage1_head_authority_document(head)
                ),
            ),
            transition=request.transition,
            stage=request.stage,
            authority_at=request.authority_at,
            prior_head=head,
            clock_started_at=request.clock_started_at,
            required_child_expectations=request.required_child_expectations,
        )

        with pytest.raises(Stage1LifecycleHeadInvariantError, match="prior head"):
            derive_stage1_lifecycle_proof(drifted)

    def test_root_topology_from_another_family_is_rejected(self) -> None:
        request = _plan_root_request()
        cross_family = Stage1LifecycleProofRequest(
            identity=request.identity,
            internal_key=request.internal_key,
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.INLINE_NONZERO_POINTER,
                SameTransactionTerminalSource(INLINE_FLOW, ADMITTED_EVENT_ID),
            ),
            transition=request.transition,
            stage=request.stage,
            authority_at=request.authority_at,
            prior_head=None,
            required_child_expectations=request.required_child_expectations,
        )

        with pytest.raises(Stage1LifecycleHeadInvariantError, match="does not belong"):
            derive_stage1_lifecycle_proof(cross_family)


class TestFlowClockPropagation:
    def test_a_successor_wire_event_repeats_the_root_clock_under_an_advanced_wall(self) -> None:
        """Prevent the live "flow clock changed" projector rejection.

        The reducer requires every successor to repeat the applied root's clock, so a
        later wall instant must never leak into ``clock_started_at``.
        """
        root = derive_stage1_lifecycle_proof(_plan_root_request())
        head = root.resulting_head
        assert head is not None
        successor = derive_stage1_lifecycle_proof(_plan_successor_request(head))

        root_event = event_from_document(json.loads(root.events[0].payload))
        successor_event = event_from_document(json.loads(successor.events[0].payload))
        assert root_event.clock_started_at == NOW
        assert successor_event.clock_started_at == NOW
        assert successor_event.occurred_at == LATER

        opened = reduce_event(None, root_event)
        assert opened.kind is ReductionKind.APPLY
        applied = reduce_event(opened.snapshot, successor_event)
        assert applied.kind is ReductionKind.APPLY

    def test_a_successor_without_the_root_clock_is_refused(self) -> None:
        root = derive_stage1_lifecycle_proof(_plan_root_request())
        head = root.resulting_head
        assert head is not None
        request = _plan_successor_request(head)

        with pytest.raises(Stage1LifecycleHeadInvariantError, match="durable root clock"):
            replace(request, clock_started_at=None)

    def test_a_root_with_a_supplied_clock_is_refused(self) -> None:
        """The root's clock is always its own ``occurred_at``.

        A stale supplied clock would propagate through ``flow_root_clock`` to every successor.
        """
        with pytest.raises(Stage1LifecycleHeadInvariantError, match="never supplied"):
            replace(_plan_root_request(), clock_started_at=NOW)


class TestRetainedEventSchemaAgreement:
    @pytest.mark.parametrize(
        "request_builder",
        [_plan_root_request, _unit_root_request, _pointer_request],
        ids=["plan", "unit", "pointer"],
    )
    def test_written_event_item_round_trips_through_the_existing_reader(
        self, request_builder: Any
    ) -> None:
        proof = derive_stage1_lifecycle_proof(request_builder())
        store = DynamoStage1LifecycleHeadStore("publications-fixture", cast("Any", _FakeDynamo()))

        items = store.transaction_items(proof)
        put = cast("dict[str, Any]", items.event_puts[0])
        written = cast("dict[str, Any]", put["Put"]["Item"])

        reread = _retained_event_request(written)
        assert reread == proof.events[0].reattest
        verified = reattest_stage1_event(reread)
        assert (verified.flow_id, verified.execution_id, verified.event_id) == (
            proof.flow_id,
            proof.execution_id,
            proof.event_id,
        )
        assert verified.authority_digest == proof.authority_digest

    def test_outbox_attributes_follow_the_committed_order_formula(self) -> None:
        proof = derive_stage1_lifecycle_proof(_plan_root_request())
        fragment = proof.events[0]
        outbox = fragment.outbox

        document = json.loads(fragment.payload)
        assert outbox.immutable_payload_digest == canonical_authority_digest(document)
        assert outbox.outbox_order is not None
        epoch_text, opaque = outbox.outbox_order.split("#")
        assert (int(epoch_text), "UTC epoch microseconds") == (
            1_789_821_296_789_012,
            "UTC epoch microseconds",
        )
        assert (len(epoch_text), "ASCII digits") == (20, "ASCII digits")
        assert opaque == canonical_authority_digest(
            [proof.partition_key, fragment.record_key, outbox.immutable_payload_digest]
        )
        assert outbox.authority_kind.value == "lifecycle_event"
        assert outbox.outbox_state.value == "pending"
        assert (outbox.consecutive_failed_delivery_attempts, "failed attempts") == (
            0,
            "failed attempts",
        )

    def test_payload_document_carries_the_persisted_public_identity(self) -> None:
        proof = derive_stage1_lifecycle_proof(_unit_root_request())
        document = cast("dict[str, Any]", json.loads(proof.events[0].payload))

        assert document["flow_id"] == UNIT_FLOW
        assert document["execution_id"] == UNIT_EXECUTION
        assert document["surface"] == Stage1Surface.UNIT.value
        assert document["authority"]["kind"] == "write_plan_unit"
        assert document["authority"]["opaque_digest"] == proof.authority_digest
        assert document["progress"] == {"completed": 0, "total": 1, "unit": "table_unit"}


class TestCompoundReplacement:
    def test_compound_produces_two_contiguous_events_and_one_head_advance(self) -> None:
        old_head = _resulting_head(derive_stage1_lifecycle_proof(_plan_root_request()))

        proof = compound_replacement_proof(_compound_request(old_head))

        assert (len(proof.events), "events") == (2, "events")
        terminal, successor = (fragment.reattest.proof for fragment in proof.events)
        assert (terminal.transition_sequence, successor.transition_sequence) == (2, 3)
        assert terminal.execution_id == old_head.current_execution_id
        assert terminal.predecessor_execution_id == old_head.predecessor_execution_id
        assert terminal.predecessor_event_id == old_head.latest_event_id
        assert successor.execution_id != old_head.current_execution_id
        assert successor.predecessor_execution_id == old_head.current_execution_id
        assert successor.predecessor_event_id == terminal.event_id
        head = _resulting_head(proof)
        assert (head.latest_transition_sequence, "head sequence") == (3, "head sequence")
        assert head.latest_event_id == successor.event_id
        assert head.current_internal_execution_id == "delivery-execution-successor"
        assert head.predecessor_execution_id == old_head.current_execution_id
        assert (head.parent_flow_id, head.causation_event_id) == (
            old_head.parent_flow_id,
            old_head.causation_event_id,
        )
        store = DynamoStage1LifecycleHeadStore("publications-fixture", cast("Any", _FakeDynamo()))
        items = store.transaction_items(proof)
        assert (len(items.event_puts), "event puts") == (2, "event puts")
        assert items.head_write is not None

    def test_compound_replay_rules_accept_identical_or_advanced_heads_only(self) -> None:
        old_head = _resulting_head(derive_stage1_lifecycle_proof(_plan_root_request()))
        proof = compound_replacement_proof(_compound_request(old_head))
        compound_head = _resulting_head(proof)

        identical = classify_stage1_replay(proof, _byte_identical_observation(proof))
        assert identical is Stage1ReplayClassification.BYTE_IDENTICAL

        next_proof = derive_stage1_lifecycle_proof(
            _plan_successor_request(
                compound_head,
                selector="delivery-execution-successor",
                authority_at=LATER + timedelta(seconds=1),
            )
        )
        advanced_head = _resulting_head(next_proof)
        chain = (reattest_stage1_event(next_proof.events[0].reattest),)
        advanced = classify_stage1_replay(
            proof,
            Stage1ReplayObservation(
                _byte_identical_observation(proof).persisted_events, advanced_head, chain
            ),
        )
        assert advanced is Stage1ReplayClassification.ADVANCED_THROUGH_CONTIGUOUS_CHAIN

        broken_chain = classify_stage1_replay(
            proof,
            Stage1ReplayObservation(
                _byte_identical_observation(proof).persisted_events, advanced_head, ()
            ),
        )
        assert broken_chain is Stage1ReplayClassification.CONFLICT

    def test_mutated_event_or_missing_head_is_a_conflict_never_a_replay(self) -> None:
        proof = derive_stage1_lifecycle_proof(_plan_root_request())
        observation = _byte_identical_observation(proof)
        persisted = observation.persisted_events[0]
        assert persisted is not None

        mutated = Stage1ReplayObservation(
            (
                Stage1PersistedEventObservation(
                    persisted.reattest, persisted.payload + " ", persisted.immutable_payload_digest
                ),
            ),
            proof.resulting_head,
        )
        assert classify_stage1_replay(proof, mutated) is Stage1ReplayClassification.CONFLICT
        headless = Stage1ReplayObservation(observation.persisted_events, None)
        assert classify_stage1_replay(proof, headless) is Stage1ReplayClassification.CONFLICT


@guard_reserved_words
@dataclass
class _FakeDynamo:
    """A conditional recording store: puts honour their condition expressions."""

    items: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    transactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @staticmethod
    def _key(key: dict[str, Any]) -> tuple[str, str]:
        return key["partition_key"]["S"], key["record_key"]["S"]

    def get_item(self, **request: Any) -> dict[str, object]:
        item = self.items.get(self._key(cast("dict[str, Any]", request["Key"])))
        return {} if item is None else {"Item": item}

    def query(self, **request: Any) -> dict[str, object]:
        values = cast("dict[str, Any]", request["ExpressionAttributeValues"])
        partition = values[":partition"]["S"]
        prefix = values[":prefix"]["S"]
        found = [
            item
            for (item_partition, record_key), item in self.items.items()
            if item_partition == partition and record_key.startswith(prefix)
        ]
        return {"Items": found}

    def transact_write_items(self, **request: Any) -> dict[str, object]:
        self.transactions.append(request)
        actions = cast("list[dict[str, Any]]", request["TransactItems"])
        allowed: list[bool] = []
        for action in actions:
            put = cast("dict[str, Any]", action["Put"])
            existing = self.items.get(self._key(put["Item"]))
            allowed.append(self._put_allowed(existing, put))
        if not all(allowed):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "fixture"},
                    "CancellationReasons": [
                        {"Code": "None" if passed else "ConditionalCheckFailed"}
                        for passed in allowed
                    ],
                },
                "TransactWriteItems",
            )
        for action in actions:
            put = cast("dict[str, Any]", action["Put"])
            self.items[self._key(put["Item"])] = dict(put["Item"])
        return {}

    @staticmethod
    def _put_allowed(existing: dict[str, Any] | None, put: dict[str, Any]) -> bool:
        condition = cast("str | None", put.get("ConditionExpression"))
        values = cast("dict[str, Any]", put.get("ExpressionAttributeValues", {}))
        if condition is None:
            return True
        if " OR " in condition:
            return existing is None or (
                existing.get("payload") == values[":expected_payload"]
                and existing.get("immutable_payload_digest") == values[":expected_digest"]
            )
        if condition == "attribute_not_exists(partition_key)":
            return existing is None
        if existing is None:
            return False
        return all(_term_holds(existing, term, values) for term in condition.split(" AND "))


def _term_holds(existing: dict[str, Any], term: str, values: dict[str, Any]) -> bool:
    if term.startswith("attribute_not_exists("):
        return term.removeprefix("attribute_not_exists(").removesuffix(")") not in existing
    name, _, placeholder = term.partition(" = ")
    return existing.get(name) == values[placeholder]


def _store(client: _FakeDynamo) -> DynamoStage1LifecycleHeadStore:
    return DynamoStage1LifecycleHeadStore("publications-fixture", cast("Any", client))


class TestAdapterApply:
    def test_apply_persists_event_and_head_once_then_replays_byte_identically(self) -> None:
        client = _FakeDynamo()
        store = _store(client)
        proof = derive_stage1_lifecycle_proof(_plan_root_request())

        first = store.apply(proof)
        replay = store.apply(proof)

        assert first.kind is Stage1LifecycleApplyKind.APPLIED
        assert replay.kind is Stage1LifecycleApplyKind.REPLAYED_BYTE_IDENTICAL
        assert (len(client.transactions), "transactions") == (1, "transactions")
        head_item = client.items[(proof.partition_key, "HEAD")]
        assert head_item["latest_event_id"]["S"] == proof.event_id
        assert head_item["latest_transition_sequence"]["N"] == "1"

    def test_apply_replays_without_mutation_after_a_later_contiguous_head_advance(self) -> None:
        client = _FakeDynamo()
        store = _store(client)
        root = derive_stage1_lifecycle_proof(_plan_root_request())
        store.apply(root)
        successor = derive_stage1_lifecycle_proof(_plan_successor_request(_resulting_head(root)))
        store.apply(successor)

        replay = store.apply(root)

        assert replay.kind is Stage1LifecycleApplyKind.REPLAYED_AFTER_ADVANCE
        assert (len(client.transactions), "transactions") == (2, "transactions")
        head_item = client.items[(root.partition_key, "HEAD")]
        assert head_item["latest_transition_sequence"]["N"] == "2"

    def test_apply_raises_the_typed_invariant_on_a_mutated_persisted_event(self) -> None:
        client = _FakeDynamo()
        store = _store(client)
        proof = derive_stage1_lifecycle_proof(_plan_root_request())
        store.apply(proof)
        client.items[(proof.partition_key, proof.events[0].record_key)]["payload"] = {
            "S": "tampered"
        }

        with pytest.raises(Stage1LifecycleHeadInvariantError, match="conflicting committed"):
            store.apply(proof)

    def test_conditional_loss_rereads_and_returns_only_a_committed_winner_conflict(self) -> None:
        client = _FakeDynamo()
        store = _store(client)
        root = derive_stage1_lifecycle_proof(_plan_root_request())
        store.apply(root)
        head = _resulting_head(root)
        winner = derive_stage1_lifecycle_proof(
            _plan_successor_request(head, selector="delivery-execution-retry")
        )
        store.apply(winner)
        loser = derive_stage1_lifecycle_proof(
            _plan_successor_request(
                head,
                selector="delivery-execution-fixture",
                authority_at=LATER + timedelta(seconds=2),
            )
        )

        with pytest.raises(Stage1LifecycleHeadInvariantError, match="conflicting committed"):
            store.apply(loser)

        assert (len(client.transactions), "transactions") == (3, "transactions")
        head_item = client.items[(root.partition_key, "HEAD")]
        assert head_item["latest_event_id"]["S"] == winner.event_id

    def test_pointer_proof_is_headless_under_its_publication_partition(self) -> None:
        client = _FakeDynamo()
        store = _store(client)
        proof = derive_stage1_lifecycle_proof(_pointer_request())

        items = store.transaction_items(proof)
        applied = store.apply(proof)

        assert items.head_write is None
        assert (len(items.event_puts), "event puts") == (1, "event puts")
        assert proof.resulting_head is None
        assert proof.head_record_key is None
        assert proof.partition_key == PUBLICATION_PARTITION
        assert (proof.flow_id, proof.execution_id) == (POINTER_FLOW, POINTER_EXECUTION)
        assert applied.kind is Stage1LifecycleApplyKind.APPLIED
        assert (proof.partition_key, proof.events[0].record_key) in client.items
        assert (proof.partition_key, "HEAD") not in client.items

    def test_pointer_request_refuses_head_or_missing_publication_partition(self) -> None:
        request = _pointer_request()
        with pytest.raises(Stage1LifecycleHeadInvariantError, match="publication partition"):
            Stage1LifecycleProofRequest(
                identity=request.identity,
                internal_key=request.internal_key,
                topology=request.topology,
                transition=request.transition,
                stage=request.stage,
                authority_at=request.authority_at,
                prior_head=None,
            )
        with pytest.raises(Stage1LifecycleHeadInvariantError, match="belongs only to pointer"):
            Stage1LifecycleProofRequest(
                identity=PlanIdentityRequest(PLAN_DIGEST, "delivery-execution-fixture"),
                internal_key=(PLAN_KEY,),
                topology=_plan_root_request().topology,
                transition=_plan_root_request().transition,
                stage="projection_plan_recorded",
                authority_at=NOW,
                prior_head=None,
                required_child_expectations=_plan_expectations(),
                publication_partition_key=PUBLICATION_PARTITION,
            )
