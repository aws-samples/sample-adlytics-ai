# SPDX-License-Identifier: MIT-0
"""Outbound port for release: ordinal, reservations, grant, apply and lineage (slice S4).

Same conventions as the dispatch port: every read is strongly consistent and every write is one
conditioned transaction. A lost race raises `GraphGuardError` with the typed signal of the first
failed member, and the caller re-reads and decides again. A grant refused because `H(U)` moved
past the run's retrieval raises `GraphGuardError(INPUT_REVISION_SUPERSEDED)`; one refused on the
proof or ordinal raises `GraphGuardError(RELEASE_GRANT_RESERVATION_PROOF_REQUIRED)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from domain.operations.execution_graph_dispatch import (
    GraphGuardError,
    NodeState,
    ServingAuthorityIdentity,
    ServingCandidate,
    ServingFrontierVector,
)
from domain.operations.execution_graph_release import (
    CONSUMER_EFFECT_NAMESPACE,
    BakeRoster,
    BindingKind,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    DurableBakeEffectV3,
    DurableBakeSourceManifest,
    LineageHead,
    LineageStep,
    PrecomputeInputV1,
    ReservationSet,
    ServingBinding,
    ServingOutcome,
)
from domain.operations.status import canonical_authority_digest
from domain.presentation.preset_artifacts import PresetArtifactPointer
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    DueItem,
    NodeRecord,
    NodeSpec,
    RunRecord,
)

__all__ = [
    "POINTER_REVISION_EXISTS",
    "ActivateServingWinnerRequest",
    "ApplyCommit",
    "BakeFanOutRequest",
    "BakeFinalizeOutcome",
    "BakeFinalizeRequest",
    "BakeGraphStore",
    "BakeSourcePinOutcome",
    "BakeSourcePinRequest",
    "BakeTaskFailure",
    "BlockRecord",
    "DrainFinalAliasAndActivateNextRequest",
    "DrainFinalAliasAndActivateNextResult",
    "GrantCommit",
    "GrantRecord",
    "GraphReleaseStore",
    "LegacyHeadReader",
    "PinnedBakeSources",
    "PointerRevisionExistsError",
    "PostGrantFailure",
    "PrecomputeReadinessOutcome",
    "PrecomputeReadinessRequest",
    "ReleaseOrderHead",
    "RetiredRelease",
    "ServingActivationResult",
    "ServingAliasDrainRequest",
    "ServingAliasDrainResult",
    "ServingBootstrapApplyResult",
    "ServingCandidateRead",
    "ServingDirectDrainRequest",
    "ServingDomainBootstrapRequest",
    "ServingDomainElectionResult",
    "ServingDomainHead",
    "ServingDomainReadResult",
    "ServingElectionOutcome",
    "ServingExecutionContext",
    "ServingGraphReleaseStore",
    "ServingMutationResult",
    "ServingOutcomeReconciliationRequest",
    "ServingOutcomeReconciliationResult",
    "ServingPromoterTransition",
    "SupersededGrant",
    "UnarmedPrecomputeSettlement",
]


@dataclass(frozen=True, slots=True)
class ReleaseOrderHead:
    """`RELEASE_ORDER#<domain>/HEAD`: one monotonic active ordinal per co-release domain."""

    domain: str
    revision: int
    """0 when the head item is absent."""
    next_ordinal: int
    active_ordinal: int | None = None
    active_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class BlockRecord:
    """`BLOCK#<domain>/BLOCK`: the co-release domain is blocked until owner review (BF-4)."""

    domain: str
    reason: str
    run_id: str
    grant_id: str | None
    blocked_at: str


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """`GRANT#<grant_id>/GRANT`: the one immutable release grant of a run."""

    grant_id: str
    run_id: str
    domain: str
    conflict_unit: str
    ordinal: int
    set_id: str
    proof_digest: str
    retrieved_at: str
    head_at_grant: str | None
    """`H(U)` the grant was checked against (graph and legacy), for the evidence."""
    lineage_sequence: int
    recorded_at: str


@dataclass(frozen=True, slots=True)
class GrantCommit:
    """The grant transaction: grant, `grant_id` on `RUN#`, domain head, lineage seal, `APPLY#`."""

    run: RunRecord
    grant: GrantRecord
    reservation: ReservationSet
    order: ReleaseOrderHead
    lineage: LineageStep
    lineage_head: LineageHead
    graph_head_epoch: int | None
    """The graph head the grant read, ConditionChecked unchanged-or-lower (r >= H)."""


@dataclass(frozen=True, slots=True)
class SupersededGrant:
    """A grant refused on `H(U)` (V-28): FENCING `older_than_current`, ordinal released."""

    run: RunRecord
    order: ReleaseOrderHead | None
    """The domain head when this run holds its active ordinal, else None."""
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ApplyCommit:
    """Grant apply: `H` advanced, run GRANTED, slot released, POST_GRANT nodes armed."""

    run: RunRecord
    grant: GrantRecord
    order: ReleaseOrderHead
    grant_apply: NodeRecord | None
    """The run's `grant_apply` node, settled SUCCEEDED in the same transaction."""
    post_grant: tuple[NodeSpec, ...]
    graph_head_epoch: int | None
    recorded_at: str


@dataclass(frozen=True, slots=True)
class PostGrantFailure:
    """A post-grant lineage failure: the domain BLOCKED, the candidate failed (BF-4)."""

    run: RunRecord
    grant: GrantRecord
    slot_owned: bool
    reason: str
    recorded_at: str
    order: ReleaseOrderHead | None
    """The domain head when this run still holds its active ordinal, else None. The failure
    returns it to idle, so clearing the block at owner review is all that restores release."""


@dataclass(frozen=True, slots=True)
class RetiredRelease:
    """A run that left OPEN before any grant: its held ordinal returned, its trigger retired.

    Without this, a run cancelled, replaced, fenced or paused after taking the domain's ordinal
    would keep `RELEASE_ORDER#<domain>/HEAD` and every other run of the domain would wait.
    """

    run: RunRecord
    """The run as read; the transaction ConditionChecks its state and revision unchanged."""
    order: ReleaseOrderHead | None
    """The domain head when this run holds its active ordinal, else None."""
    retire_trigger: bool
    """True once the run is terminal. A PAUSED run keeps `RELEASE#` so resume carries on."""


@dataclass(frozen=True, slots=True)
class ServingDomainHead:
    """The versioned serving-domain head read with its complete frontier vector."""

    domain: str
    state: str
    version: int
    cohort_revision: str | None = None
    serving_input_revision: str | None = None
    winner_run_id: str | None = None
    direct_binding_id: str | None = None
    direct_grant_id: str | None = None
    elected_frontier: ServingFrontierVector | None = None
    elected_frontier_digest: str | None = None
    elected_candidate_digest: str | None = None
    elected_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServingCandidateRead:
    """One fully verified settled publication and its immutable serving authority."""

    candidate: ServingCandidate
    identity: ServingAuthorityIdentity
    target_digest: str


@dataclass(frozen=True, slots=True)
class ServingExecutionContext:
    """The active cohort identity every winner contribution must match."""

    head: ServingDomainHead
    run_ids: tuple[str, ...]
    plan_digest: str


@dataclass(frozen=True, slots=True)
class ServingDomainReadResult:
    """One strong read of the domain head and complete cumulative live frontier."""

    head: ServingDomainHead | None
    frontier: ServingFrontierVector | None


@dataclass(frozen=True, slots=True)
class ServingDomainBootstrapRequest:
    """Grant apply joined to replay-safe domain/head/frontier initialization."""

    apply: ApplyCommit
    head: ServingDomainHead
    frontier: ServingFrontierVector

    @property
    def identity(self) -> ServingAuthorityIdentity:
        """The applied run's serving authority identity."""
        return ServingAuthorityIdentity(
            self.apply.run.run_id,
            self.apply.grant.grant_id,
            self.apply.grant.ordinal,
            self.apply.grant.domain,
        )


class ServingBootstrapApplyResult(StrEnum):
    """The domain bootstrap/apply write or its exact replay."""

    APPLIED = "applied"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class ServingPromoterTransition:
    """The immutable `SERVING_TRANSITION#` replay record of one final-alias drain."""

    domain: str
    drained_cohort_revision: str
    aws_request_id: str
    drained_at: str
    elected_cohort_revision: str | None = None
    elected_at: str | None = None


class ServingElectionOutcome(StrEnum):
    """The closed per-domain outcome of one rolling promoter attempt."""

    ELECTED = "elected"
    DRAIN_INCOMPLETE = "drain_incomplete"
    WAITING_LOWER_ORDINAL = "waiting_lower_ordinal"
    FRONTIER_RACE = "frontier_race"
    NO_ELIGIBLE = "no_eligible"


@dataclass(frozen=True, slots=True)
class ActivateServingWinnerRequest:
    """Activate one winner on an IDLE head with its immutable elected snapshot."""

    head: ServingDomainHead
    frontier: ServingFrontierVector
    candidates: tuple[ServingCandidate, ...]
    winner: ServingCandidate
    cohort_revision: str
    serving_input_revision: str
    recorded_at: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class ServingActivationResult:
    """The committed winner activation."""

    mutation: ServingMutationResult


@dataclass(frozen=True, slots=True)
class ServingDirectDrainRequest:
    """Bind and drain the winner without owning a terminal clock instant."""

    head: ServingDomainHead
    direct: ServingBinding
    winner: ServingCandidate


@dataclass(frozen=True, slots=True)
class ServingAliasDrainRequest:
    """Write one immutable non-final alias and drain its serving obligations."""

    head: ServingDomainHead
    direct: ServingBinding
    alias: ServingBinding
    candidate: ServingCandidate
    identity: ServingAuthorityIdentity
    terminal_at: str
    remaining_run_ids: tuple[str, ...]
    aws_request_id: str

    def __post_init__(self) -> None:
        if self.direct.binding_kind is not BindingKind.DIRECT:
            raise ValueError("an alias drain requires an already-durable direct binding.")
        if (
            self.alias.binding_kind is not BindingKind.ALIAS
            or self.alias.direct_binding_id != self.direct.binding_id
        ):
            raise ValueError("an alias drain must reference its exact direct binding.")
        expected = ServingAuthorityIdentity(
            self.candidate.run_id,
            self.candidate.grant_id,
            self.candidate.grant_ordinal,
            self.candidate.domain,
        )
        if self.identity != expected:
            raise ValueError("an alias drain requires the exact candidate authority identity.")


class ServingMutationResult(StrEnum):
    """A serving transition committed now or was proven as its byte-equivalent replay."""

    COMMITTED = "committed"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class DrainFinalAliasAndActivateNextRequest:
    """The fixed-member final-alias drain and successor activation, one transaction."""

    head: ServingDomainHead
    direct: ServingBinding
    final_alias: ServingBinding
    final_candidate: ServingCandidate
    identity: ServingAuthorityIdentity
    final_obligation: DueItem
    terminal_at: str
    live_frontier: ServingFrontierVector
    successor_candidates: tuple[ServingCandidate, ...]
    successor_candidate_digest: str
    successor_winner: ServingCandidate | None
    successor_cohort_revision: str | None
    successor_input_revision: str | None
    successor_plan_digest: str | None
    aws_request_id: str

    def __post_init__(self) -> None:
        if self.final_alias.direct_binding_id != self.direct.binding_id:
            raise ValueError("the final alias must reference its exact direct binding.")
        if bool(self.successor_winner) != bool(self.successor_cohort_revision):
            raise ValueError("successor winner and cohort revision are both present or absent.")


@dataclass(frozen=True, slots=True)
class DrainFinalAliasAndActivateNextResult:
    """The committed or replayed final transition and the resulting head."""

    mutation: ServingMutationResult
    outcome: ServingElectionOutcome
    transition: ServingPromoterTransition
    activated_head: ServingDomainHead | None


@dataclass(frozen=True, slots=True)
class ServingDomainElectionResult:
    """One domain's typed outcome within a promoter pass."""

    domain: str
    outcome: ServingElectionOutcome
    drained_cohort_revision: str | None = None
    elected_cohort_revision: str | None = None
    transition: ServingPromoterTransition | None = None


@dataclass(frozen=True, slots=True)
class ServingOutcomeReconciliationRequest:
    """The exact authority snapshot whose aggregate serving outcome is reconciled."""

    domain: str
    direct: ServingBinding
    aliases: tuple[ServingBinding, ...]
    authority_digest: str


@dataclass(frozen=True, slots=True)
class ServingOutcomeReconciliationResult:
    """One authority-derived aggregate outcome and its exact snapshot digest."""

    outcome: ServingOutcome
    direct_binding_id: str
    alias_count: int
    authority_digest: str


class LegacyHeadReader(Protocol):
    """The legacy head, derived from immutable legacy authority (BF-8, legacy head)."""

    def head_after(self, window: str, r: datetime) -> datetime | None:
        """Return the largest newer retrieval of the window whose legacy plan COMPLETED."""
        ...

    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        """Row 9 in epoch 1: the exact version's completed legacy plan key, or None."""
        ...


class ServingGraphReleaseStore(Protocol):
    """Rolling serving-domain authority operations."""

    def read_serving_domain(self, domain: str) -> ServingDomainReadResult:
        """Strongly read the domain head and every live-frontier shard."""
        ...

    def read_serving_candidate(self, run_id: str) -> ServingCandidateRead | None:
        """Read one settled-eligible candidate with matching ARM/COMPLETE authority."""
        ...

    def read_serving_execution(self, grant_id: str, run_id: str) -> ServingExecutionContext | None:
        """Read the winner context stamped on its bake node at activation."""
        ...

    def bootstrap_serving_domain_and_apply_grant(
        self, request: ServingDomainBootstrapRequest
    ) -> ServingBootstrapApplyResult:
        """Apply a grant while creating or conditioning the domain head and frontier."""
        ...

    def activate_serving_winner(
        self, request: ActivateServingWinnerRequest
    ) -> ServingActivationResult:
        """Activate one winner with an immutable elected snapshot."""
        ...

    def drain_direct_binding(self, request: ServingDirectDrainRequest) -> ServingMutationResult:
        """Persist the direct binding's drain against the immutable head."""
        ...

    def drain_alias_binding(self, request: ServingAliasDrainRequest) -> ServingAliasDrainResult:
        """Drain one non-final alias."""
        ...

    def drain_final_alias_and_activate_next(
        self, request: DrainFinalAliasAndActivateNextRequest
    ) -> DrainFinalAliasAndActivateNextResult:
        """Drain the final alias and elect the successor in one replay-safe transaction."""
        ...

    def read_serving_promoter_transition(
        self, domain: str, drained_cohort_revision: str
    ) -> ServingPromoterTransition | None:
        """Strongly read the drained cohort's immutable transition record."""
        ...

    def reconcile_serving_outcome(
        self, request: ServingOutcomeReconciliationRequest
    ) -> ServingOutcomeReconciliationResult:
        """Return the aggregate serving outcome of one authority snapshot."""
        ...


@dataclass(frozen=True, slots=True)
class ServingAliasDrainResult:
    """The committed non-final alias drain."""

    mutation: ServingMutationResult


@dataclass(frozen=True, slots=True)
class UnarmedPrecomputeSettlement:
    """ADR-140: settle a skipped-finalizer winner's precompute and bind its cohort, together.

    One transaction: the run, the ACTIVE head and the stored bake member as read; the new
    `NO_ELIGIBLE` precompute member; the `WAITING_INPUT` precompute node moved to `SUCCEEDED`
    with the consumer's effect record; and the direct binding.
    """

    run: RunRecord
    head: ServingDomainHead
    bake: CohortContribution
    precompute: CohortContribution
    node: NodeRecord
    """The winner's `precompute` node, `WAITING_INPUT`: it owns no token, lock or outbox."""
    binding: ServingBinding
    effect_record: str
    effect_digest: str
    recorded_at: str

    def __post_init__(self) -> None:
        grant = self.run.grant_id
        if self.node.name != CohortMember.PRECOMPUTE.value or self.node.run_id != self.run.run_id:
            raise ValueError("an unarmed settlement names the winner's precompute node.")
        if self.node.state is not NodeState.WAITING_INPUT:
            raise ValueError("an unarmed settlement moves only a WAITING_INPUT precompute node.")
        if (
            self.precompute.member is not CohortMember.PRECOMPUTE
            or self.precompute.outcome is not CohortOutcome.NO_ELIGIBLE
            or self.precompute.artifact_revision is not None
            or self.precompute.grant_id != grant
        ):
            raise ValueError("an unarmed settlement contributes precompute NO_ELIGIBLE only.")
        if self.bake.member is not CohortMember.BAKE or self.bake.grant_id != grant:
            raise ValueError("an unarmed settlement conditions on the grant's bake member.")
        if self.binding.binding_kind is not BindingKind.DIRECT or self.binding.grant_id != grant:
            raise ValueError("an unarmed settlement writes the grant's direct binding.")
        if self.head.state != "ACTIVE" or self.head.winner_run_id != self.run.run_id:
            raise ValueError("an unarmed settlement is written only for the ACTIVE winner.")
        if (
            canonical_authority_digest([CONSUMER_EFFECT_NAMESPACE, self.effect_record])
            != self.effect_digest
        ):
            raise ValueError("an unarmed settlement's effect digest does not re-hash.")


class GraphReleaseStore(Protocol):
    """The release authority over the execution-graph table."""

    def read_release_order(self, domain: str) -> ReleaseOrderHead:
        """Read the domain's release-order head (absent reads revision 0, idle)."""
        ...

    def read_block(self, domain: str) -> BlockRecord | None:
        """Read the domain's block record."""
        ...

    def read_graph_head(self, conflict_unit: str) -> tuple[datetime | None, int | None]:
        """Read `HEAD#<U>`: its retrieval instant and its comparable epoch seconds."""
        ...

    def read_lineage(self, conflict_unit: str) -> LineageHead:
        """Read `LINEAGE#<U>/HEAD` (absent reads sequence 0, no tail)."""
        ...

    def read_grant(self, grant_id: str) -> GrantRecord | None:
        """Read one grant."""
        ...

    def read_reserved_targets(self, reservation: ReservationSet) -> tuple[str, ...]:
        """Return the set's targets that hold this set's reservation."""
        ...

    def proof_digest(self, reservation: ReservationSet) -> str | None:
        """Return the set's `TARGET_RESERVATION_PROOF` digest, or None."""
        ...

    def acquire_ordinal(
        self, order: ReleaseOrderHead, run: RunRecord, reservation: ReservationSet
    ) -> None:
        """CAS the idle domain head to this run's ordinal and create its reservation set."""
        ...

    def reserve_target(self, reservation: ReservationSet, target: str) -> None:
        """Append this ordinal's reservation to one target's tail (strictly monotonic)."""
        ...

    def seal_proof(self, reservation: ReservationSet) -> None:
        """Write the proof after ConditionChecking every target's reservation."""
        ...

    def issue_grant(self, commit: GrantCommit) -> None:
        """Commit the grant transaction (BF-1, V-28, V-29)."""
        ...

    def refuse_superseded(self, refusal: SupersededGrant) -> None:
        """Fence a run whose retrieval is older than `H(U)` and release its ordinal (V-28)."""
        ...

    def retire_release(self, retired: RetiredRelease) -> None:
        """Return a no-longer-OPEN, ungranted run's ordinal and retire its terminal trigger."""
        ...

    def apply_grant(self, commit: ApplyCommit) -> None:
        """Grant apply (BF-8): release point 2."""
        ...

    def record_post_grant_failure(self, failure: PostGrantFailure) -> None:
        """Block the domain and fail the candidate after an apply-time refusal (BF-4)."""
        ...

    def complete_granted_run(  # noqa: PLR0913 -- the design-exact keyword-only terminal_at
        self,
        run: RunRecord,
        nodes: tuple[NodeRecord, ...],
        drain: ServingDirectDrainRequest,
        *,
        terminal_at: str,
    ) -> ServingMutationResult:
        """Move a GRANTED run to SUCCEEDED once every POST_GRANT node is terminal (S6 §2.8).

        One transaction: the winner's direct drain (binding terminal stamp on the exact stored
        binding, cohort roster, head, `ARM#`/`COMPLETE#`/marker/frontier) and the run's
        `serving_outcome`. A refused completion leaves `COMPLETE#` for the next sweep.
        """
        ...

    def record_contribution(self, contribution: CohortContribution) -> None:
        """Write one sibling's immutable cohort contribution (idempotent on equal content)."""
        ...

    def read_contributions(self, grant_id: str) -> tuple[CohortContribution, ...]:
        """Read every contribution of one grant's cohort."""
        ...

    def bind_serving(self, binding: ServingBinding) -> None:
        """Write the immutable serving binding of one settled cohort."""
        ...

    def read_serving_binding(self, grant_id: str) -> ServingBinding | None:
        """Read one grant's serving binding."""
        ...

    def settle_unarmed_precompute(
        self, settlement: UnarmedPrecomputeSettlement
    ) -> ServingMutationResult:
        """ADR-140: settle precompute `NO_ELIGIBLE` and bind the cohort in one transaction.

        A moved condition raises `GraphGuardError`. An exact stored post-image is `REPLAY`; a
        lost response whose image is neither pre nor post is `GRAPH_PRECOMPUTE/6`.
        """
        ...


@dataclass(frozen=True, slots=True)
class BakeSourcePinRequest:
    """Plan 4.3 (S5, DB21): pin a winner's source manifest once, before any partition.

    Conditioned on the bake fan-out claim's lease, the run's grant and the domain head that
    names this run the ACTIVE winner at this version and input revision. `reads` holds each
    roster row's encoded recorded reads, in roster order.
    """

    run: RunRecord
    claim: ClaimRecord
    head: ServingDomainHead
    manifest: DurableBakeSourceManifest
    reads: tuple[bytes, ...]
    recorded_at: str

    def __post_init__(self) -> None:
        if len(self.reads) != len(self.manifest.tasks):
            raise ValueError("a bake source pin stores one read blob per roster row.")
        if self.head.winner_run_id != self.run.run_id or self.head.state != "ACTIVE":
            raise ValueError("a bake source pin is written only by the ACTIVE winner.")


class BakeSourcePinOutcome(StrEnum):
    """How one pin attempt ended."""

    PINNED = "pinned"
    """This attempt committed the manifest."""
    ADOPTED = "adopted"
    """The exact manifest was already stored (a replay, or an uncertain response's post-image)."""


@dataclass(frozen=True, slots=True)
class PinnedBakeSources:
    """A winner's stored pin: the manifest and each row's encoded reads, in roster order."""

    manifest: DurableBakeSourceManifest
    reads: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class BakeFanOutRequest:
    """Plan 4.6: arm every roster task on its lane, and put the finalizer, in one transaction.

    Conditioned on the fan-out claim, the grant, the ACTIVE winner head and the stored pin's
    digest, so no task outbox exists before the pin commits (S5).
    """

    run: RunRecord
    claim: ClaimRecord
    head: ServingDomainHead
    manifest_digest: str
    tasks: tuple[NodeSpec, ...]
    finalize: NodeSpec
    arm_attributes: dict[str, str | int]
    """The serving identity the re-put `ARM#` (scope serving) carries, as `COMPLETE#` does."""
    recorded_at: str


@dataclass(frozen=True, slots=True)
class BakeTaskFailure:
    """Plan 4.6 (F9): a task settles FAILED with its `BAKE_RECOVERY#` obligation, together."""

    run: RunRecord
    node: NodeRecord
    claim: ClaimRecord
    reason: str
    """`fatal` or `retry_exhausted` (contract `bake_v3.recovery_reasons`)."""
    failure_code: str
    recorded_at: str


class BakeFinalizeOutcome(StrEnum):
    """How one V3 final transaction ended."""

    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"
    """The exact post-image was already stored: a replay or a lost response."""


POINTER_REVISION_EXISTS: Final = "pointer_revision_exists"
"""The guard signal, abandon cause, node `failure_code` and log outcome of a final transaction
refused only on existing identical history revisions (ADR-140 amendment 2026-10-28)."""


class PointerRevisionExistsError(GraphGuardError):
    """The final transaction was refused only because written pages' revisions already exist.

    Every failed condition was a history put whose stored record names the same page, variant,
    revision, body digest and custody digest under another grant. The one guard a caller may
    treat as deterministic: the finalizer abandons on its final receive (ADR-140 amendment
    2026-10-28). Any caller without that branch handles it as the base guard.
    """

    def __init__(self, existing: tuple[PresetArtifactPointer, ...]) -> None:
        if not existing:
            raise ValueError("a pointer revision refusal names at least one existing revision.")
        super().__init__(POINTER_REVISION_EXISTS)
        self.existing = existing
        """The desired pointers whose revision already exists, in transaction order."""


class PrecomputeReadinessOutcome(StrEnum):
    """How the exact bake-effect readiness transaction ended."""

    ARMED = "armed"
    ADOPTED = "adopted"


@dataclass(frozen=True, slots=True)
class PrecomputeReadinessRequest:
    """DB25: arm precompute only from the exact winner's complete roster effects."""

    run: RunRecord
    claim: ClaimRecord
    head: ServingDomainHead
    node: NodeRecord
    roster: BakeRoster
    tasks: tuple[NodeRecord, ...]
    effects: tuple[DurableBakeEffectV3, ...]
    input: PrecomputeInputV1
    recorded_at: str

    def __post_init__(self) -> None:
        if self.node.name != "precompute" or self.node.run_id != self.run.run_id:
            raise ValueError("precompute readiness names the winner's precompute node.")
        if self.head.winner_run_id != self.run.run_id or self.head.state != "ACTIVE":
            raise ValueError("precompute readiness is written only by the ACTIVE winner.")
        keys = tuple(effect.key for effect in self.effects)
        if (
            keys != self.roster.keys
            or keys != tuple(entry.key for entry in self.input.entries)
            or len(self.tasks) != len(keys)
        ):
            raise ValueError("precompute readiness effects and input roster differ.")
        projected = PrecomputeInputV1.from_effects(self.effects)
        for expected, actual in zip(projected.entries, self.input.entries, strict=True):
            if (
                expected.page,
                expected.variant,
                expected.effect_kind,
                expected.source_manifest_digest,
                expected.artifact_revision,
                expected.output_manifest_digest,
                expected.reason_code,
            ) != (
                actual.page,
                actual.variant,
                actual.effect_kind,
                actual.source_manifest_digest,
                actual.artifact_revision,
                actual.output_manifest_digest,
                actual.reason_code,
            ):
                raise ValueError("precompute readiness input differs from its source effect.")


@dataclass(frozen=True, slots=True)
class BakeFinalizeRequest:
    """Plan 4.6 under A3: the winner's one final transaction (12 + N + 3 x written members).

    It conditions every roster effect and the exact precompute contribution, writes only the
    page pointers of written effects and the immutable FINALIZED receipt with the direct binding,
    head/frontier drain and run completion. It names no semantic `GENERATION#` member.
    """

    run: RunRecord
    claim: ClaimRecord
    node: NodeRecord
    """The `bake_finalize` node, CLAIMED by this finalizer."""
    tasks: tuple[NodeRecord, ...]
    effects: tuple[DurableBakeEffectV3, ...]
    pointer_items: tuple[tuple[object, ...], ...]
    """Each written effect's pointer-table members, in roster order."""
    precompute: CohortContribution
    binding: ServingBinding
    head: ServingDomainHead
    receipt_digest: str
    effect_record: str
    recorded_at: str
    readiness_digest: str
    """The readiness input's digest, the stamp `COMPLETE#` carries (ADR-140: a stale precompute
    may carry another digest, or none)."""


class BakeGraphStore(Protocol):
    """The V3 bake's graph authority (plan 4.3, 4.6): pin, fan-out, task failure, finalize."""

    def read_durable_bake_sources(self, run_id: str) -> PinnedBakeSources | None:
        """Strongly read a winner's source pin."""
        ...

    def pin_durable_bake_sources(self, request: BakeSourcePinRequest) -> BakeSourcePinOutcome:
        """Pin a winner's manifest, N counts and digest once."""
        ...

    def mark_bake_selection(self, claim: ClaimRecord) -> None:
        """Record, lease-fenced, that the fan-out's one selection began."""
        ...

    def fan_out_bake_tasks(self, request: BakeFanOutRequest) -> None:
        """Arm every roster task and put the finalizer, all or none."""
        ...

    def settle_bake_task_failure(self, failure: BakeTaskFailure) -> None:
        """Settle a task FAILED with its BAKE_RECOVERY# obligation."""
        ...

    def arm_precompute_from_bake_effects(
        self, request: PrecomputeReadinessRequest
    ) -> PrecomputeReadinessOutcome:
        """Write exact readiness and its one precompute outbox atomically."""
        ...

    def read_precompute_input(self, run_id: str) -> PrecomputeInputV1 | None:
        """Read and attest the winner's exact readiness input."""
        ...

    def finalize_bake(self, request: BakeFinalizeRequest) -> BakeFinalizeOutcome:
        """Commit the winner's one final transaction.

        Raises `PointerRevisionExistsError` when every failed condition is an identical existing
        history revision, and the base `GraphGuardError` for any other refusal.
        """
        ...

    def release_finalize_runtime(self, node: NodeRecord) -> None:
        """Delete the finalizer's token and lock after its commit (replayable)."""
        ...
