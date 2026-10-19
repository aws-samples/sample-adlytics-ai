# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 operational-status identities, authority, recovery, and reduction."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from domain.operations.stage1_operational_status import (
    ALL_STAGE1_SURFACES,
    AthenaUsage,
    BlockErrorClass,
    BlockStage1OutboxRequest,
    ChildOutcomeV2,
    DeliverStage1AuthorityRequest,
    DependencyErrorClass,
    EventIdentityRequest,
    GenerationHeadDecisionKind,
    GenerationHeadDecisionRequest,
    InlineIdentityRequest,
    LatestSelectorDecisionKind,
    LatestSelectorDecisionRequest,
    LegitimateRootSource,
    LifecycleEventAuthority,
    MarkStage1OutboxDeliveredRequest,
    OperationalDataFlowUsageContribution,
    OperationalDataFlowUsageEvidenceDisposition,
    OutboxState,
    OwnerDisposition,
    PlanAdmissionSource,
    PlanIdentityRequest,
    PlanReleaseDecisionKind,
    PlanReleaseRequest,
    PlanRequiredChildExpectations,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    ProtectedStage1OutboxCase,
    ProviderTerminalState,
    ReattestStage1EventRequest,
    RecordStage1AuthorityObservationRequest,
    RecordStage1OutboxDeliveryFailureRequest,
    RecoveryOutcome,
    RequeueStage1OutboxRequest,
    RequiredChildExpectation,
    RootDiscriminator,
    SameTransactionTerminalSource,
    SemanticAttemptSuccessSource,
    SettledUsageContributionAuthority,
    SettledUsageEvidenceDispositionAuthority,
    SettleStage1AthenaUsageRequest,
    Stage1AuthorityKind,
    Stage1AuthorityReattestationError,
    Stage1EventAuthorityRequest,
    Stage1FlowFamily,
    Stage1OutboxAuthoritySnapshot,
    Stage1OutboxOrderRequest,
    Stage1OutboxRecoveryInvocation,
    Stage1OutboxRecoveryResult,
    Stage1OwnerTransition,
    Stage1RetainedEventProof,
    Stage1Surface,
    Stage1TopologyRequest,
    TopologySourceKind,
    UnitIdentityRequest,
    UsageCategory,
    affected_surfaces,
    block_stage1_outbox,
    decide_generation_head,
    decide_latest_selector,
    decide_plan_release,
    deliver_stage1_authority,
    derive_event_id,
    derive_inline_identity,
    derive_plan_identity,
    derive_pointer_identity,
    derive_stage1_topology,
    derive_unit_identity,
    event_authority_digest,
    event_authority_tuple,
    reattest_stage1_event,
    settle_stage1_athena_usage,
    stage1_authority_time,
    stage1_outbox_order,
    verify_stage1_wire_member,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    AuthorityObservation,
    CompletionOutcome,
    EvaluationBinding,
    Evidence,
    FlowState,
    OperationalDataFlowEvent,
    OperationalStatusError,
    canonical_authority_bytes,
    canonical_authority_digest,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

NOW = datetime(2026, 9, 19, 12, 34, 56, 789012, tzinfo=UTC)
DIGEST = "a" * 64
CASE_REF = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PLAN_FLOW_ID = "11111111-1111-4111-8111-111111111111"
CHILD_FLOW_ID = "22222222-2222-4222-8222-222222222222"
CHILD_EVENT_ID = "33333333-3333-4333-8333-333333333333"
CHILD_EXECUTION_ID = "44444444-4444-4444-8444-444444444444"


def _binding(surface: Stage1Surface) -> EvaluationBinding:
    return EvaluationBinding(MEASUREMENT_REFS[surface.value])


def _lifecycle_event() -> OperationalDataFlowEvent:
    surface = Stage1Surface.PLAN
    return OperationalDataFlowEvent(
        event_id="55555555-5555-4555-8555-555555555555",
        predecessor_event_id=None,
        flow_id=PLAN_FLOW_ID,
        execution_id="66666666-6666-4666-8666-666666666666",
        predecessor_execution_id=None,
        transition_sequence=1,
        surface=surface.value,
        stage="projection_plan_terminal",
        state=FlowState.COMPLETED,
        occurred_at=NOW,
        authority=AuthorityObservation("write_plan_meta", DIGEST, NOW),
        evaluation_binding=_binding(surface),
        outcome=CompletionOutcome.SUCCEEDED,
    )


def _pending_snapshot(
    kind: Stage1AuthorityKind = Stage1AuthorityKind.LIFECYCLE_EVENT,
) -> Stage1OutboxAuthoritySnapshot:
    record_key = "EVENT#fixture" if kind is Stage1AuthorityKind.LIFECYCLE_EVENT else "USAGE#fixture"
    order = stage1_outbox_order(
        Stage1OutboxOrderRequest("WRITE_PLAN#fixture", record_key, DIGEST, NOW)
    )
    return Stage1OutboxAuthoritySnapshot(
        partition_key="WRITE_PLAN#fixture",
        record_key=record_key,
        immutable_payload_digest=DIGEST,
        authority_kind=kind,
        outbox_state=OutboxState.PENDING,
        consecutive_failed_delivery_attempts=0,
        outbox_order=order,
    )


def _settlement(**changes: object) -> SettleStage1AthenaUsageRequest:
    values: dict[str, object] = {
        "attempt_id": "attempt-fixture",
        "flow_id": CHILD_FLOW_ID,
        "execution_id": CHILD_EXECUTION_ID,
        "evaluation_binding": _binding(Stage1Surface.UNIT),
        "owner_disposition": OwnerDisposition.SUCCESSFUL,
        "observed_at": NOW,
        "provider_terminal_state": ProviderTerminalState.SUCCEEDED,
        "scanned_bytes": 0,
        "engine_ms": 0,
    }
    values.update(changes)
    return SettleStage1AthenaUsageRequest(**values)  # type: ignore[arg-type]


def _contribution() -> OperationalDataFlowUsageContribution:
    authority = settle_stage1_athena_usage(_settlement()).authority
    assert isinstance(authority, OperationalDataFlowUsageContribution)
    return authority


def _outcome(
    *, generation: int = 7, sequence: int = 7, family: Stage1FlowFamily = Stage1FlowFamily.UNIT
) -> ChildOutcomeV2:
    return ChildOutcomeV2(
        parent_flow_id=PLAN_FLOW_ID,
        child_flow_id=CHILD_FLOW_ID,
        child_event_id=CHILD_EVENT_ID,
        child_transition_sequence=sequence,
        child_execution_id=CHILD_EXECUTION_ID,
        owner_generation=generation,
        lifecycle_state=FlowState.COMPLETED,
        completion_outcome=CompletionOutcome.SUCCEEDED,
        child_family=family,
    )


class TestCanonicalAuthorityAndIdentity:
    def test_unicode_map_order_fixture_matches_committed_bytes_and_digest(self) -> None:
        first = {"b": "é😀", "a": ["東京", 1]}
        second = {"a": ["東京", 1], "b": "é😀"}
        expected = b'{"a":["\\u6771\\u4eac",1],"b":"\\u00e9\\ud83d\\ude00"}'

        assert canonical_authority_bytes(first) == canonical_authority_bytes(second) == expected
        assert (len(expected), "UTF-8 bytes") == (49, "UTF-8 bytes")
        assert expected.hex() == (
            # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
            "7b2261223a5b225c75363737315c7534656163222c315d2c2262223a225c7530306539"
            # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
            "5c75643833645c7564653030227d"
        )
        assert canonical_authority_digest(first) == (
            # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
            "e5538692fcf9b5b794a29e642174ec8d27eb8027187a517d26432456c3dc599c"
        )

    def test_nonfinite_authority_values_are_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="not canonical JSON"):
            canonical_authority_bytes({"quantity": float("nan")})

    def test_plan_unit_and_inline_fixture_uuids_are_exact(self) -> None:
        plan = derive_plan_identity(
            PlanIdentityRequest("plan-fixture", "delivery-execution-fixture")
        )
        unit = derive_unit_identity(
            UnitIdentityRequest("plan-fixture", "unit-fixture", "writer-execution-fixture")
        )
        inline = derive_inline_identity(
            InlineIdentityRequest(
                "ingest_delivery",
                "operation-fixture",
                "physical-table-fixture",
                "inline-execution-fixture",
            )
        )

        assert (plan.surface, plan.flow_id, plan.execution_id) == (
            Stage1Surface.PLAN,
            "68cb3192-c252-5fed-bbe8-8f0ab98f74b6",
            "76db826e-6da1-57ba-b170-75d627f053a3",
        )
        assert (unit.surface, unit.flow_id, unit.execution_id) == (
            Stage1Surface.UNIT,
            "960cfeca-0bad-57fd-b4b0-383e15d59085",
            "ef642f3b-65f1-580f-b01b-e2b7102b98f9",
        )
        assert (inline.surface, inline.flow_id, inline.execution_id) == (
            Stage1Surface.INLINE_RUN,
            "ac30cd31-fad5-5dcb-a821-fe91c54eafc0",
            "8a2fdf66-8d89-517a-88c2-7f09be54ef7b",
        )

    @pytest.mark.parametrize(
        ("kind", "internal_id", "execution_id"),
        [
            (
                PointerExecutionSourceKind.QUEUED_UNIT_WRITER_EXECUTION,
                "writer-execution-fixture",
                "bf1bc15f-7b72-54db-8c7a-50682ce6810e",
            ),
            (
                PointerExecutionSourceKind.INLINE_RUN_EXECUTION,
                "inline-execution-fixture",
                "6f03579d-6e36-5f49-a906-e69c979df18e",
            ),
            (
                PointerExecutionSourceKind.SEMANTIC_PUBLICATION_ATTEMPT,
                "attempt-automatic-fixture",
                "7251cadf-6717-57b7-836e-fe92905d346c",
            ),
            (
                PointerExecutionSourceKind.SEMANTIC_PUBLICATION_ATTEMPT,
                "attempt-transitional-fixture",
                "816f564b-1294-5293-939f-536f6ba247cf",
            ),
            (
                PointerExecutionSourceKind.SEMANTIC_PUBLICATION_ATTEMPT,
                "attempt-manual-fixture",
                "9b38b5d0-eb39-5686-ae6a-0893727e3a04",
            ),
            (
                PointerExecutionSourceKind.SEMANTIC_PUBLICATION_ATTEMPT,
                "attempt-reprocess-fixture",
                "1a45fed2-aada-5d0c-bdfa-f22b1874c34f",
            ),
        ],
    )
    def test_pointer_fixture_uses_the_exact_closed_execution_selector(
        self,
        kind: PointerExecutionSourceKind,
        internal_id: str,
        execution_id: str,
    ) -> None:
        identity = derive_pointer_identity(
            PointerIdentityRequest(
                "fictional.dataset|2026-09-19|whole",
                "publication-fixture",
                7,
                PointerExecutionSource(kind, internal_id),
            )
        )

        assert identity.family is Stage1FlowFamily.POINTER
        assert identity.surface is Stage1Surface.POINTER
        assert identity.flow_id == "e4de2f19-58db-5f51-b924-b4e5118d4db3"
        assert identity.execution_id == execution_id


class TestTopology:
    @pytest.mark.parametrize(
        ("transition", "discriminator"),
        [
            (
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                RootDiscriminator.AUTOMATIC_INGESTION_PLAN,
            ),
            (
                Stage1OwnerTransition.FIRST_TRANSITIONAL_INLINE_RECORDED,
                RootDiscriminator.TRANSITIONAL_DELIVERY_INLINE,
            ),
            (
                Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
                RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE,
            ),
            (
                Stage1OwnerTransition.FIRST_REPROCESS_INLINE_RECORDED,
                RootDiscriminator.REPROCESS_SEMANTIC_V2_OPERATOR_INLINE,
            ),
            (
                Stage1OwnerTransition.FIRST_GRAPH_UNIT_INLINE_RECORDED,
                RootDiscriminator.GRAPH_UNIT_INLINE,
            ),
        ],
    )
    def test_committed_root_matrix_has_no_relationship_or_digest(
        self, transition: Stage1OwnerTransition, discriminator: RootDiscriminator
    ) -> None:
        topology = derive_stage1_topology(
            Stage1TopologyRequest(transition, LegitimateRootSource(discriminator))
        )

        assert topology.topology_source_kind is TopologySourceKind.LEGITIMATE_ROOT
        assert topology.root_discriminator is discriminator
        assert (topology.parent_flow_id, topology.causation_event_id) == (None, None)
        assert topology.topology_source_digest is None

    def test_prior_plan_admission_and_pointer_topology_use_exact_proof_variants(self) -> None:
        prior = derive_stage1_topology(
            Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_PLAN_EVENT,
                PriorOwnerEventSource(None, CHILD_EVENT_ID, {"head": "complete"}),
            )
        )
        admission = derive_stage1_topology(
            Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_UNIT_STARTED,
                PlanAdmissionSource(
                    PLAN_FLOW_ID,
                    CHILD_EVENT_ID,
                    {"meta": "exact"},
                    {"head": "complete"},
                    {"event": "admitted"},
                ),
            )
        )
        nonzero = derive_stage1_topology(
            Stage1TopologyRequest(
                Stage1OwnerTransition.QUEUED_NONZERO_POINTER,
                SameTransactionTerminalSource(CHILD_FLOW_ID, CHILD_EVENT_ID),
            )
        )
        zero = derive_stage1_topology(
            Stage1TopologyRequest(
                Stage1OwnerTransition.ZERO_ROW_POINTER,
                SemanticAttemptSuccessSource(
                    CHILD_FLOW_ID, CHILD_EVENT_ID, {"attempt_history": ["success"]}
                ),
            )
        )

        assert (prior.parent_flow_id, prior.causation_event_id) == (None, CHILD_EVENT_ID)
        assert prior.topology_source_digest == canonical_authority_digest(
            ["stage1-topology-prior-owner-event/v1", {"head": "complete"}]
        )
        assert (admission.parent_flow_id, admission.causation_event_id) == (
            PLAN_FLOW_ID,
            CHILD_EVENT_ID,
        )
        assert admission.topology_source_digest is not None
        assert nonzero.topology_source_digest is None
        assert zero.topology_source_digest == canonical_authority_digest(
            [
                "stage1-topology-semantic-attempt-success/v1",
                {"attempt_history": ["success"]},
            ]
        )

    def test_cross_variant_topology_is_rejected(self) -> None:
        with pytest.raises(OperationalStatusError, match="does not match"):
            derive_stage1_topology(
                Stage1TopologyRequest(
                    Stage1OwnerTransition.ZERO_ROW_POINTER,
                    SameTransactionTerminalSource(CHILD_FLOW_ID, CHILD_EVENT_ID),
                )
            )

    @pytest.mark.parametrize(
        "discriminator",
        [item for item in RootDiscriminator if item is not RootDiscriminator.GRAPH_UNIT_INLINE],
    )
    def test_s6_d5_a_graph_unit_root_with_another_discriminator_is_refused(
        self, discriminator: RootDiscriminator
    ) -> None:
        # S6 §2.5 (D5): the graph-unit inline writer has exactly one legitimate root. The
        # paired control is the `GRAPH_UNIT_INLINE` row of the committed root matrix above.
        with pytest.raises(OperationalStatusError, match="does not match the owner transition"):
            derive_stage1_topology(
                Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_GRAPH_UNIT_INLINE_RECORDED,
                    LegitimateRootSource(discriminator),
                )
            )

    def test_s6_d5_the_graph_unit_discriminator_roots_no_other_writer_transition(self) -> None:
        with pytest.raises(OperationalStatusError, match="does not match the owner transition"):
            derive_stage1_topology(
                Stage1TopologyRequest(
                    Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
                    LegitimateRootSource(RootDiscriminator.GRAPH_UNIT_INLINE),
                )
            )


class TestUsageSettlementAndDelivery:
    def test_complete_provider_evidence_preserves_zero_and_owner_category(self) -> None:
        authority = settle_stage1_athena_usage(_settlement()).authority

        assert isinstance(authority, OperationalDataFlowUsageContribution)
        assert authority.category is UsageCategory.PRODUCTIVE
        assert authority.usage == AthenaUsage(query_count=1, scanned_bytes=0, engine_ms=0)
        replay = settle_stage1_athena_usage(_settlement()).authority
        assert isinstance(replay, OperationalDataFlowUsageContribution)
        assert authority.contribution_id == replay.contribution_id

    def test_missing_provider_quantity_creates_one_final_unavailable_disposition(self) -> None:
        authority = settle_stage1_athena_usage(_settlement(engine_ms=None)).authority

        assert isinstance(authority, OperationalDataFlowUsageEvidenceDisposition)
        assert authority.missing_quantities == ("engine_ms",)
        assert authority.known_usage.query_count == 1
        assert authority.known_usage.scanned_bytes == 0
        assert authority.known_usage.engine_ms is None
        assert authority.evidence is Evidence.UNAVAILABLE

    @pytest.mark.parametrize(
        ("changes", "category"),
        [
            ({"owner_disposition": OwnerDisposition.NO_CHANGE}, UsageCategory.NO_CHANGE),
            ({"owner_disposition": OwnerDisposition.SUPERSEDED}, UsageCategory.SUPERSEDED),
            ({"owner_disposition": OwnerDisposition.RETRYABLE_FAILURE}, UsageCategory.RETRY),
            (
                {"owner_disposition": OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE},
                UsageCategory.UNAVAILABLE,
            ),
            (
                {
                    "owner_disposition": OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE,
                    "recovery_or_redrive_execution": True,
                },
                UsageCategory.RETRY,
            ),
        ],
    )
    def test_durable_owner_disposition_fixes_usage_category(
        self, changes: dict[str, object], category: UsageCategory
    ) -> None:
        authority = settle_stage1_athena_usage(_settlement(**changes)).authority

        assert isinstance(authority, OperationalDataFlowUsageContribution)
        assert authority.category is category

    def test_no_accepted_request_creates_no_usage_authority(self) -> None:
        result = settle_stage1_athena_usage(
            _settlement(
                accepted=False,
                owner_disposition=OwnerDisposition.NO_ACCEPTED_REQUEST,
                provider_terminal_state=None,
                scanned_bytes=None,
                engine_ms=None,
            )
        )

        assert result.authority is None

    @pytest.mark.parametrize(
        "changes",
        [
            {"resource": "lambda"},
            {"athena_usage_state": "settled"},
            {"provider_terminal_state": None},
            {"reconciles_disposition_id": CASE_REF},
            {"is_delta": True},
        ],
    )
    def test_non_athena_unsettled_terminal_and_delta_invariants_fail_closed(
        self, changes: dict[str, object]
    ) -> None:
        with pytest.raises(OperationalStatusError):
            _settlement(**changes)

    def test_delivery_maps_contribution_to_one_queue_member_and_mark_request(self) -> None:
        contribution = _contribution()
        snapshot = _pending_snapshot(Stage1AuthorityKind.USAGE_CONTRIBUTION)

        result = deliver_stage1_authority(
            DeliverStage1AuthorityRequest(
                snapshot,
                NOW,
                usage_contribution=SettledUsageContributionAuthority(contribution),
            )
        )

        assert result.send_request.message_group_id == contribution.flow_id
        assert result.send_request.message_deduplication_id == contribution.contribution_id
        assert result.mark_delivered_request == MarkStage1OutboxDeliveredRequest(snapshot, NOW)

    def test_ambiguous_and_prefix_kind_mismatched_authority_is_rejected(self) -> None:
        lifecycle = LifecycleEventAuthority(_lifecycle_event())
        contribution = SettledUsageContributionAuthority(_contribution())
        with pytest.raises(OperationalStatusError, match="exactly one"):
            DeliverStage1AuthorityRequest(
                _pending_snapshot(),
                NOW,
                lifecycle_event=lifecycle,
                usage_contribution=contribution,
            )
        with pytest.raises(OperationalStatusError, match="prefix/kind/member"):
            deliver_stage1_authority(
                DeliverStage1AuthorityRequest(
                    _pending_snapshot(Stage1AuthorityKind.USAGE_CONTRIBUTION),
                    NOW,
                    lifecycle_event=lifecycle,
                )
            )

    def test_unsettled_usage_and_reconciliation_authority_cannot_be_delivered(self) -> None:
        contribution = _contribution()
        with pytest.raises(OperationalStatusError, match="unsettled"):
            SettledUsageContributionAuthority(contribution, athena_usage_state="unsettled")
        with pytest.raises(OperationalStatusError, match="deltas"):
            SettledUsageContributionAuthority(contribution, is_delta=True)
        with pytest.raises(OperationalStatusError, match="deltas"):
            replace(contribution, reconciles_disposition_id=CASE_REF)

    def test_final_unavailable_disposition_has_no_reconciliation_shape(self) -> None:
        disposition = settle_stage1_athena_usage(_settlement(engine_ms=None)).authority
        assert isinstance(disposition, OperationalDataFlowUsageEvidenceDisposition)

        result = deliver_stage1_authority(
            DeliverStage1AuthorityRequest(
                _pending_snapshot(Stage1AuthorityKind.USAGE_EVIDENCE_DISPOSITION),
                NOW,
                usage_evidence_disposition=SettledUsageEvidenceDispositionAuthority(disposition),
            )
        )

        assert result.send_request.message_deduplication_id == disposition.disposition_id
        assert result.send_request.message_group_id == disposition.flow_id


class TestOutboxAndRecovery:
    def test_outbox_order_is_stable_and_time_keyed(self) -> None:
        request = Stage1OutboxOrderRequest("PK", "EVENT#fixture", DIGEST, NOW)
        order = stage1_outbox_order(request)

        assert order == stage1_outbox_order(request)
        epoch_text, opaque_digest = order.split("#")
        assert (len(epoch_text), "ASCII digits") == (20, "ASCII digits")
        assert (len(opaque_digest), "lowercase hex digits") == (64, "lowercase hex digits")
        assert int(epoch_text) == 1_789_821_296_789_012

    def test_observation_is_lifecycle_only_and_all_requests_require_pending_authority(self) -> None:
        usage = _pending_snapshot(Stage1AuthorityKind.USAGE_CONTRIBUTION)
        with pytest.raises(OperationalStatusError, match="only lifecycle"):
            RecordStage1AuthorityObservationRequest(usage, NOW)

        terminal = replace(
            _pending_snapshot(), outbox_state=OutboxState.DELIVERED, outbox_order=None
        )
        with pytest.raises(OperationalStatusError, match="requires pending"):
            MarkStage1OutboxDeliveredRequest(terminal, NOW)

    def test_dependency_failure_series_is_zero_one_two_then_blocked_three(self) -> None:
        zero = _pending_snapshot()
        first = RecordStage1OutboxDeliveryFailureRequest(
            zero,
            NOW,
            DependencyErrorClass.QUEUE_SEND_FAILED,
        )
        one = replace(zero, consecutive_failed_delivery_attempts=1)
        second = RecordStage1OutboxDeliveryFailureRequest(
            one,
            NOW,
            DependencyErrorClass.DELIVERY_MARK_FAILED,
        )
        two = replace(zero, consecutive_failed_delivery_attempts=2)
        block_request = BlockStage1OutboxRequest(
            two,
            NOW,
            BlockErrorClass.QUEUE_SEND_FAILED,
            Stage1Surface.PLAN,
            CASE_REF,
        )

        assert first.authority.consecutive_failed_delivery_attempts == 0
        assert second.authority.consecutive_failed_delivery_attempts == 1
        disposition = block_stage1_outbox(block_request)
        assert disposition.final_failed_delivery_attempts == 3
        assert disposition.reason_code == "stage1_outbox_blocked_dependency"
        assert disposition.evidence is Evidence.INCOMPLETE

    def test_invalid_authority_blocks_immediately_with_safe_four_surface_fallback(self) -> None:
        request = BlockStage1OutboxRequest(
            _pending_snapshot(),
            NOW,
            BlockErrorClass.PREFIX_KIND_MISMATCH,
            None,
            CASE_REF,
        )

        disposition = block_stage1_outbox(request)

        assert disposition.final_failed_delivery_attempts == 0
        assert disposition.reason_code == "stage1_outbox_blocked_invalid"
        assert disposition.evidence is Evidence.UNAVAILABLE
        assert (
            disposition.affected_surfaces
            == ALL_STAGE1_SURFACES
            == (
                Stage1Surface.INLINE_RUN,
                Stage1Surface.PLAN,
                Stage1Surface.UNIT,
                Stage1Surface.POINTER,
            )
        )
        assert affected_surfaces(Stage1Surface.UNIT) == (Stage1Surface.UNIT,)

    def test_requeue_derives_the_exact_marker_from_the_protected_case(self) -> None:
        blocked = replace(_pending_snapshot(), outbox_state=OutboxState.BLOCKED, outbox_order=None)
        case = ProtectedStage1OutboxCase(
            CASE_REF,
            blocked,
            (Stage1Surface.PLAN, Stage1Surface.UNIT),
            NOW,
            "stage1_outbox_blocked_dependency",
        )
        request = RequeueStage1OutboxRequest(case, NOW, "dependency resolved")

        assert request.marker.case_ref == CASE_REF
        assert request.marker.affected_surfaces == (
            Stage1Surface.PLAN,
            Stage1Surface.UNIT,
        )

    def test_recovery_contract_accepts_only_uuid4_sanitized_reason_and_closed_outcome(self) -> None:
        invocation = Stage1OutboxRecoveryInvocation(CASE_REF, "dependency resolved")
        result = Stage1OutboxRecoveryResult(CASE_REF, RecoveryOutcome.REQUEUED)

        assert (invocation.schema_version, result.schema_version) == ("1", "1")
        assert result.outcome is RecoveryOutcome.REQUEUED
        with pytest.raises(OperationalStatusError, match="UUIDv4"):
            Stage1OutboxRecoveryInvocation(
                "68cb3192-c252-5fed-bbe8-8f0ab98f74b6", "dependency resolved"
            )
        with pytest.raises(OperationalStatusError, match="sanitized"):
            Stage1OutboxRecoveryInvocation(CASE_REF, "line one\nline two")

    def test_time_digest_and_prefix_validation_fail_closed(self) -> None:
        with pytest.raises(OperationalStatusError, match="timezone-aware"):
            Stage1OutboxOrderRequest(
                "PK",
                "EVENT#fixture",
                DIGEST,
                datetime(2026, 9, 19),  # noqa: DTZ001 -- deliberate invalid instant
            )
        with pytest.raises(OperationalStatusError, match="canonical lowercase sha256"):
            Stage1OutboxOrderRequest("PK", "EVENT#fixture", DIGEST.upper(), NOW)
        with pytest.raises(OperationalStatusError, match="unknown Stage-1 authority kind"):
            Stage1OutboxAuthoritySnapshot(
                "PK",
                "USAGE#fixture",
                DIGEST,
                "unknown",  # type: ignore[arg-type]
                OutboxState.PENDING,
                0,
                f"{'0' * 20}#{DIGEST}",
            )
        with pytest.raises(OperationalStatusError, match="prefix and kind disagree"):
            Stage1OutboxAuthoritySnapshot(
                "PK",
                "EVENT#fixture",
                DIGEST,
                Stage1AuthorityKind.USAGE_CONTRIBUTION,
                OutboxState.PENDING,
                0,
                f"{'0' * 20}#{DIGEST}",
            )


class TestChildOutcomeAndPlanRelease:
    def test_committed_outcome_fixture_pins_integer_keys_bytes_and_digest(self) -> None:
        outcome = _outcome()
        encoded = canonical_authority_bytes(
            [
                "operational-data-flow-child-outcome/v2",
                PLAN_FLOW_ID,
                CHILD_FLOW_ID,
                CHILD_EVENT_ID,
                7,
                CHILD_EXECUTION_ID,
                7,
                "completed",
                "succeeded",
            ]
        )

        assert outcome.partition_key == f"FLOW#{PLAN_FLOW_ID}"
        assert outcome.history_sort_key == (
            f"CHILD_OUTCOME_V2#{CHILD_FLOW_ID}#00000000000000000007"
        )
        assert outcome.generation_head_sort_key == (
            f"CHILD_OUTCOME_GENERATION_HEAD_V2#{CHILD_FLOW_ID}#00000000000000000007"
        )
        assert outcome.latest_selector_sort_key == f"CHILD_OUTCOME_LATEST_V2#{CHILD_FLOW_ID}"
        assert (len(encoded), "canonical UTF-8 bytes") == (226, "canonical UTF-8 bytes")
        assert outcome.outcome_digest == (
            # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
            "03c5405af6fcc02c643a572d8f308cd0f29a31a4bb7cdaaf1295359a8695e440"
        )

    def test_generation_head_advances_only_same_generation_contiguous_history(self) -> None:
        current = _outcome(sequence=7)
        candidate = replace(
            current,
            child_event_id="77777777-7777-4777-8777-777777777777",
            child_transition_sequence=8,
        )

        decision = decide_generation_head(GenerationHeadDecisionRequest(candidate, current, True))
        gap = decide_generation_head(
            GenerationHeadDecisionRequest(
                replace(candidate, child_transition_sequence=9), current, False
            )
        )
        wrong_generation = decide_generation_head(
            GenerationHeadDecisionRequest(replace(candidate, owner_generation=8), current, True)
        )

        assert decision.kind is GenerationHeadDecisionKind.ADVANCE
        assert gap.kind is GenerationHeadDecisionKind.CONFLICT
        assert wrong_generation.kind is GenerationHeadDecisionKind.CONFLICT

    def test_latest_selector_overtakes_when_new_generation_has_lower_sequence(
        self,
    ) -> None:
        old_generation_late_sequence = _outcome(generation=7, sequence=99)
        new_generation_early_sequence = _outcome(generation=8, sequence=1)

        decision = decide_latest_selector(
            LatestSelectorDecisionRequest(
                new_generation_early_sequence, old_generation_late_sequence
            )
        )

        assert decision.kind is LatestSelectorDecisionKind.ADVANCE
        assert decision.selected == new_generation_early_sequence

    def test_latest_selector_does_not_regress_when_an_old_generation_arrives_later(self) -> None:
        new_generation_early_sequence = _outcome(generation=8, sequence=1)
        old_generation_later_arrival = _outcome(generation=7, sequence=100)

        decision = decide_latest_selector(
            LatestSelectorDecisionRequest(
                old_generation_later_arrival, new_generation_early_sequence
            )
        )

        assert decision.kind is LatestSelectorDecisionKind.RETAIN
        assert decision.selected == new_generation_early_sequence

    def test_pointer_flows_are_headless_and_rejected_from_child_reduction_and_plan_expectations(
        self,
    ) -> None:
        with pytest.raises(OperationalStatusError, match="only queued units"):
            _outcome(family=Stage1FlowFamily.POINTER)
        with pytest.raises(OperationalStatusError, match="only queued-unit"):
            RequiredChildExpectation(CHILD_FLOW_ID, 7, Stage1FlowFamily.POINTER)

    def test_plan_release_reads_only_the_exact_expected_generation_head(self) -> None:
        expectations = PlanRequiredChildExpectations(
            PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)
        )
        generation_eight_success = _outcome(generation=8)
        generation_seven_success = _outcome(generation=7)

        awaiting = decide_plan_release(
            PlanReleaseRequest(FlowState.COMPLETED, expectations, (generation_eight_success,))
        )
        released = decide_plan_release(
            PlanReleaseRequest(FlowState.COMPLETED, expectations, (generation_seven_success,))
        )

        assert awaiting.kind is PlanReleaseDecisionKind.AWAIT_CHILDREN
        assert awaiting.unsatisfied_child_flow_ids == (CHILD_FLOW_ID,)
        assert released.kind is PlanReleaseDecisionKind.RELEASE
        assert released.unsatisfied_child_flow_ids == ()
        assert expectations.generation_head_keys == (
            (
                f"FLOW#{PLAN_FLOW_ID}",
                f"CHILD_OUTCOME_GENERATION_HEAD_V2#{CHILD_FLOW_ID}#00000000000000000007",
            ),
        )

    @pytest.mark.parametrize(
        "terminal_state",
        [FlowState.BLOCKED, FlowState.FAILED, FlowState.SUPERSEDED],
    )
    def test_noncompleted_plan_terminals_are_not_child_gated(
        self, terminal_state: FlowState
    ) -> None:
        expectations = PlanRequiredChildExpectations(
            PLAN_FLOW_ID, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)
        )

        result = decide_plan_release(PlanReleaseRequest(terminal_state, expectations, ()))

        assert result.kind is PlanReleaseDecisionKind.RELEASE
        assert result.unsatisfied_child_flow_ids == ()


COMMITTED_UNIT_AUTHORITY_TIME = "00001789200000000000"
COMMITTED_UNIT_TUPLE_JSON = (
    '["stage1-event-authority/v1","semantic_projection_unit",'
    '["WRITE_PLAN#2222#exec-g7","db.campaigns"],"semantic_unit_attested",'
    '["completed","succeeded",null],"00001789200000000000"]'
)
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
COMMITTED_UNIT_AUTHORITY_DIGEST = "504f6193e35e4826abe1a40ee6b4d9cb8ee266d23dda76f828d2ce7611adcd59"


def _unit_event_authority(
    *, transition_id: str = "semantic_unit_attested"
) -> Stage1EventAuthorityRequest:
    return Stage1EventAuthorityRequest(
        surface=Stage1Surface.UNIT,
        internal_key=("WRITE_PLAN#2222#exec-g7", "db.campaigns"),
        transition_id=transition_id,
        lifecycle_state=FlowState.COMPLETED,
        completion_outcome=CompletionOutcome.SUCCEEDED,
        typed_reason=None,
        authority_time=COMMITTED_UNIT_AUTHORITY_TIME,
    )


def _unit_identity_member() -> UnitIdentityRequest:
    return UnitIdentityRequest("plan-fixture", "db.campaigns", "writer-execution-fixture")


def _unit_proof(*, event_id: str | None = None) -> Stage1RetainedEventProof:
    derived = derive_unit_identity(_unit_identity_member())
    digest = event_authority_digest(_unit_event_authority())
    return Stage1RetainedEventProof(
        flow_id=derived.flow_id,
        execution_id=derived.execution_id,
        event_id=event_id
        or derive_event_id(
            EventIdentityRequest(Stage1Surface.UNIT, digest, 1, FlowState.COMPLETED)
        ),
        transition_sequence=1,
        predecessor_event_id=None,
        predecessor_execution_id=None,
        parent_flow_id=PLAN_FLOW_ID,
        causation_event_id=CHILD_EVENT_ID,
    )


class TestEventAuthorityReattestation:
    def test_committed_event_authority_vector_pins_bytes_length_and_digest(self) -> None:
        request = _unit_event_authority()

        encoded = canonical_authority_bytes(event_authority_tuple(request))

        assert encoded.decode("ascii") == COMMITTED_UNIT_TUPLE_JSON
        assert (len(encoded), "UTF-8 bytes") == (178, "UTF-8 bytes")
        assert event_authority_digest(request) == COMMITTED_UNIT_AUTHORITY_DIGEST

    def test_authority_time_renders_the_exact_twenty_digit_owner_instant(self) -> None:
        assert stage1_authority_time(NOW) == "00001789821296789012"
        with pytest.raises(OperationalStatusError, match="20-digit"):
            _unit_event_authority_with_time("1789200000000000")

    @pytest.mark.parametrize(
        "changes",
        [
            {"internal_key": ("WRITE_PLAN#2222#exec-g7",)},
            {"surface": Stage1Surface.PLAN},
            {"lifecycle_state": FlowState.RUNNING},
            {"completion_outcome": None},
        ],
    )
    def test_family_arity_and_terminal_outcome_invariants_fail_closed(
        self, changes: dict[str, object]
    ) -> None:
        values: dict[str, object] = {
            "surface": Stage1Surface.UNIT,
            "internal_key": ("WRITE_PLAN#2222#exec-g7", "db.campaigns"),
            "transition_id": "semantic_unit_attested",
            "lifecycle_state": FlowState.COMPLETED,
            "completion_outcome": CompletionOutcome.SUCCEEDED,
            "typed_reason": None,
            "authority_time": COMMITTED_UNIT_AUTHORITY_TIME,
        }
        values.update(changes)

        with pytest.raises(OperationalStatusError):
            Stage1EventAuthorityRequest(**values)  # type: ignore[arg-type]

    def test_pointer_internal_key_requires_the_integer_fencing_token(self) -> None:
        with pytest.raises(OperationalStatusError, match="fencing token"):
            Stage1EventAuthorityRequest(
                surface=Stage1Surface.POINTER,
                internal_key=("fictional.dataset|2026-09-19|whole", "publication-fixture", "7"),
                transition_id="semantic_projection_servable",
                lifecycle_state=FlowState.COMPLETED,
                completion_outcome=CompletionOutcome.SUCCEEDED,
                typed_reason=None,
                authority_time=COMMITTED_UNIT_AUTHORITY_TIME,
            )

    def test_reattestation_recomputes_uuids_and_returns_the_verified_authority(self) -> None:
        request = ReattestStage1EventRequest(
            _unit_event_authority(),
            _unit_identity_member(),
            _unit_proof(),
            owner_generation=7,
        )

        verified = reattest_stage1_event(request)

        derived = derive_unit_identity(_unit_identity_member())
        assert verified.authority_digest == COMMITTED_UNIT_AUTHORITY_DIGEST
        assert (verified.flow_id, verified.execution_id) == (
            derived.flow_id,
            derived.execution_id,
        )
        assert verified.event_id == _unit_proof().event_id
        assert verified.owner_generation == 7
        assert verified.lifecycle_state is FlowState.COMPLETED
        assert verified.completion_outcome is CompletionOutcome.SUCCEEDED

    def test_persisted_identity_drift_is_rejected_never_adopted(self) -> None:
        drifted = ReattestStage1EventRequest(
            _unit_event_authority(),
            _unit_identity_member(),
            _unit_proof(event_id="77777777-7777-4777-8777-777777777777"),
            owner_generation=7,
        )
        changed_transition = ReattestStage1EventRequest(
            _unit_event_authority(transition_id="semantic_unit_started"),
            _unit_identity_member(),
            _unit_proof(),
            owner_generation=7,
        )

        for request in (drifted, changed_transition):
            with pytest.raises(Stage1AuthorityReattestationError, match="state invariant"):
                reattest_stage1_event(request)

    def test_sequence_predecessor_coherence_fails_closed(self) -> None:
        derived = derive_unit_identity(_unit_identity_member())
        with pytest.raises(Stage1AuthorityReattestationError, match="state invariant"):
            Stage1RetainedEventProof(
                flow_id=derived.flow_id,
                execution_id=derived.execution_id,
                event_id=CHILD_EVENT_ID,
                transition_sequence=2,
                predecessor_event_id=None,
                predecessor_execution_id=None,
                parent_flow_id=PLAN_FLOW_ID,
                causation_event_id=None,
            )
        with pytest.raises(Stage1AuthorityReattestationError, match="state invariant"):
            Stage1RetainedEventProof(
                flow_id=derived.flow_id,
                execution_id=derived.execution_id,
                event_id=CHILD_EVENT_ID,
                transition_sequence=1,
                predecessor_event_id=CHILD_EVENT_ID,
                predecessor_execution_id=None,
                parent_flow_id=PLAN_FLOW_ID,
                causation_event_id=None,
            )

    def test_projection_fields_bind_to_their_owning_family_only(self) -> None:
        with pytest.raises(Stage1AuthorityReattestationError, match="owner generation"):
            ReattestStage1EventRequest(
                _unit_event_authority(), _unit_identity_member(), _unit_proof()
            )
        with pytest.raises(Stage1AuthorityReattestationError, match="child expectations"):
            ReattestStage1EventRequest(
                _unit_event_authority(),
                _unit_identity_member(),
                _unit_proof(),
                owner_generation=7,
                required_child_expectations=PlanRequiredChildExpectations(
                    _unit_proof().flow_id, (RequiredChildExpectation(CHILD_FLOW_ID, 7),)
                ),
            )
        with pytest.raises(Stage1AuthorityReattestationError, match="identity family"):
            ReattestStage1EventRequest(
                _unit_event_authority(),
                PlanIdentityRequest("plan-fixture", "delivery-execution-fixture"),
                _unit_proof(),
                owner_generation=7,
            )

    def test_wire_member_must_equal_the_reattested_authority(self) -> None:
        verified = reattest_stage1_event(
            ReattestStage1EventRequest(
                _unit_event_authority(),
                _unit_identity_member(),
                _unit_proof(),
                owner_generation=7,
            )
        )
        event = OperationalDataFlowEvent(
            event_id=verified.event_id,
            predecessor_event_id=None,
            flow_id=verified.flow_id,
            execution_id=verified.execution_id,
            predecessor_execution_id=None,
            transition_sequence=1,
            surface=Stage1Surface.UNIT.value,
            stage="projection_unit_terminal",
            state=FlowState.COMPLETED,
            occurred_at=NOW,
            authority=AuthorityObservation("stage1_lifecycle_head", verified.authority_digest, NOW),
            evaluation_binding=_binding(Stage1Surface.UNIT),
            outcome=CompletionOutcome.SUCCEEDED,
            parent_flow_id=PLAN_FLOW_ID,
            causation_event_id=CHILD_EVENT_ID,
        )

        verify_stage1_wire_member(event, verified)

        mismatched = replace(
            event, authority=AuthorityObservation("stage1_lifecycle_head", "b" * 64, NOW)
        )
        with pytest.raises(Stage1AuthorityReattestationError, match="state invariant"):
            verify_stage1_wire_member(mismatched, verified)


def _unit_event_authority_with_time(authority_time: str) -> Stage1EventAuthorityRequest:
    return Stage1EventAuthorityRequest(
        surface=Stage1Surface.UNIT,
        internal_key=("WRITE_PLAN#2222#exec-g7", "db.campaigns"),
        transition_id="semantic_unit_attested",
        lifecycle_state=FlowState.COMPLETED,
        completion_outcome=CompletionOutcome.SUCCEEDED,
        typed_reason=None,
        authority_time=authority_time,
    )
