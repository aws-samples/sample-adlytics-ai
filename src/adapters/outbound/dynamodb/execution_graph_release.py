# SPDX-License-Identifier: MIT-0
"""DynamoDB adapter for release: ordinal, reservations, grant, apply and lineage (S4).

Same table and keys as the S1 and S3 adapters. Items this slice adds:

- `RELEASE_ORDER#<domain>/HEAD` (one monotonic active ordinal per co-release domain) and its
  immutable `INTENT#<ordinal>` rows;
- `RESERVATION_SET#<set_id>/SET` (the complete target roster) and `/PROOF` (count and ordered
  digest, written only after every target's reservation is ConditionChecked);
- `TARGET#<target>/TAIL` (the last ordinal reserved on that target, strictly increasing) and
  `TARGET#<target>/RESERVATION#<ordinal>`;
- `GRANT#<grant_id>/GRANT`, the one immutable grant of a run;
- `LINEAGE#<U>/HEAD` and its immutable `STEP#<sequence>` rows (one-step lineage, BF-4);
- `BLOCK#<domain>/BLOCK`, the co-release domain's block (row 6 reads it, S1A-8);
- `COHORT#<grant_id>/CONTRIBUTION#<member>` and `SERVING#<grant_id>/BINDING`;
- in the run's due shard: `RELEASE#<run_id>` (the release issuer's trigger, written by the
  `materialize_raw` settlement through `build_successors`, S6 §2.2), `APPLY#<run_id>` (written by
  the grant transaction and deleted by apply: the re-drive of a committed, unapplied grant) and
  `COMPLETE#<run_id>` (S6: written by apply with the grant's domain and ordinal, deleted by a
  serving terminal: `complete_granted_run` with the winner's direct drain, or an alias or
  capacity terminal; the run's completion obligation and its serving-order place).

`HEAD#<U>/HEAD` gains `head_epoch`, the published retrieval instant in epoch seconds, so the
grant and apply transactions can ConditionCheck `r >= H` on a number.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from adapters.outbound.dynamodb.byte_oracle import MAX_CONSERVATIVE_SERVICE_ITEM_BYTES
from adapters.outbound.dynamodb.execution_graph import due_shard, root_clock_attribute
from adapters.outbound.dynamodb.execution_graph_dispatch import (
    DynamoGraphDispatchStore,
    arming_members,
    build_fence_run,
    build_successors,
    due_obligation_item,
    graph_envelope,
    node_transition_member,
    read_node_item,
    release_members,
    serving_authority_identity_attributes,
    serving_authority_identity_from_item,
)
from adapters.outbound.dynamodb.graph_transaction_bytes import (
    MAX_TRANSACTION_BYTES,
    item_bytes,
    transaction_bytes,
)
from adapters.outbound.dynamodb.graph_transactions import (
    FailedMember,
    Member,
    MemberSpec,
    TransactRequest,
    check,
    delete,
    put,
    transact,
    update,
)
from adapters.outbound.dynamodb.items import write_bool, write_int, write_text
from adapters.outbound.dynamodb.preset_artifacts import existing_history_revision
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_STALE,
    DOMAIN_LOCK_HELD,
    NODE_STATE_CHANGED,
    PROMOTE_WAKE_CHANGED,
    RUN_STATE_CHANGED,
    SLOT_NOT_OWNED,
    ArmScope,
    FenceReason,
    GraphGuardError,
    NodeState,
    ServingAuthorityIdentity,
    ServingCandidate,
    ServingFrontierVector,
    cohort_root_clock,
    provider_accounted_bytes,
    require_serving_authority_identity,
    template_node,
)
from domain.operations.execution_graph_release import (
    BAKE_TASK_NODE,
    CONFLICT_DOMAIN_BLOCKED,
    DURABLE_BAKE_FLIP_ATOMICITY_INVARIANT,
    FINALIZER_GENERATION_MEMBER_REFUSED,
    GRANT_APPLY_NODE,
    INPUT_REVISION_SUPERSEDED,
    LINEAGE_REPAIR_UNSUPPORTED,
    RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
    RELEASE_ORDER_BUSY,
    UNARMED_PRECOMPUTE_IMAGE_MIXED,
    BakeTaskSource,
    BindingKind,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    DurableBakeSourceManifest,
    DurableBakeSourceManifestInvalidError,
    LineageHead,
    PinRefusal,
    PrecomputeInputV1,
    ReservationSet,
    ServingBinding,
    aggregate_serving_outcome,
    binding_authorizes_serving,
    final_transaction_member_count,
    serving_outcome,
    written_pointer_effects,
)
from domain.operations.generated_execution_graphs import BAKE_V3, BOUNDS, SERVING_COALESCING
from domain.operations.run_request import RunState
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    FenceRunRequest,
    GraphStoreUnavailableError,
    NodeRecord,
    NodeSettlement,
    NodeSpec,
    RunRecord,
    ServingPublicationAuthority,
    SuccessorRequest,
)
from ports.outbound.execution_graph_release import (
    ActivateServingWinnerRequest,
    ApplyCommit,
    BakeFanOutRequest,
    BakeFinalizeOutcome,
    BakeFinalizeRequest,
    BakeSourcePinOutcome,
    BakeSourcePinRequest,
    BakeTaskFailure,
    BlockRecord,
    DrainFinalAliasAndActivateNextRequest,
    DrainFinalAliasAndActivateNextResult,
    GrantCommit,
    GrantRecord,
    PinnedBakeSources,
    PointerRevisionExistsError,
    PostGrantFailure,
    PrecomputeReadinessOutcome,
    PrecomputeReadinessRequest,
    ReleaseOrderHead,
    RetiredRelease,
    ServingActivationResult,
    ServingAliasDrainRequest,
    ServingAliasDrainResult,
    ServingBootstrapApplyResult,
    ServingCandidateRead,
    ServingDirectDrainRequest,
    ServingDomainBootstrapRequest,
    ServingDomainHead,
    ServingDomainReadResult,
    ServingElectionOutcome,
    ServingExecutionContext,
    ServingMutationResult,
    ServingOutcomeReconciliationRequest,
    ServingOutcomeReconciliationResult,
    ServingPromoterTransition,
    SupersededGrant,
    UnarmedPrecomputeSettlement,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        TransactWriteItemTypeDef,
    )

    from domain.presentation.preset_artifacts import PresetArtifactPointer


_BAKE_FAN_OUT_FAILED: Final = ErrorCode("GRAPH_BAKE_V3", 10)
"""`BAKE_FAN_OUT_FAILED`: the pin guard's refusal is the fan-out's existing fatal outcome."""
_SCOPE_READ: Final = "current_for_scope"
_HORIZON_TOKEN: Final = re.compile(r"^(newest=[1-9][0-9]*|from=\d{4}-\d{2}-\d{2})$")
"""A whole-scope read's pinned horizon token (`ScopeHorizon.token`, A5.1 I9)."""

__all__ = [
    "DynamoGraphReleaseStore",
    "build_publication_flip_participant",
    "head_epoch",
    "refuse_generation_members",
    "serving_binding_from_item",
    "serving_binding_key",
]

type Item = dict[str, AttributeValueTypeDef]

_OPEN: Final = "OPEN"
_SERVING_SHARDS: Final = int(SERVING_COALESCING["serving_frontier_shards"])
_SERVING_HEAD_CHANGED: Final = "SERVING_HEAD_CHANGED"
_SERVING_FRONTIER_CHANGED: Final = "SERVING_FRONTIER_CHANGED"
_SERVING_FRONTIER_PARTIAL: Final = "serving_frontier_partial"
_FINAL_AUTHORITY_MEMBERS: Final = 10
"""Rolling V3 final authority members after retiring day/frontier drain writes."""
_FIRST_POINTER_MEMBERS: Final = 2
_SUPERSEDING_POINTER_MEMBERS: Final = 3
"""A written effect's pointer members: history put and CURRENT write, plus a predecessor's
expiry when it supersedes one."""


def _serving_head_key(domain: str) -> Item:
    return _key(f"SERVING_DOMAIN#{domain}", "HEAD")


def _serving_frontier_key(domain: str, shard: int) -> Item:
    return _key(f"SERVING_FRONTIER#{domain}#{shard:02d}", "COUNTER")


def _serving_marker_key(run_id: str) -> Item:
    return _key(f"SERVING_PUBLICATION#{run_id}", "MARKER")


def _string_list(item: Item, name: str) -> tuple[str, ...]:
    return tuple(str(value.get("S", "")) for value in item.get(name, {}).get("L", []))


def head_epoch(instant: datetime) -> int:
    """Return the retrieval instant as whole epoch seconds (the key's one-second resolution)."""
    return int(instant.timestamp())


def _key(pk: str, sk: str) -> Item:
    return {"pk": write_text(pk), "sk": write_text(sk)}


def _values(**values: AttributeValueTypeDef) -> dict[str, AttributeValueTypeDef]:
    return {f":{name}": value for name, value in values.items()}


def _list_int(values: tuple[int, ...]) -> AttributeValueTypeDef:
    return {"L": [write_int(value) for value in values]}


def _list_text(values: tuple[str, ...]) -> AttributeValueTypeDef:
    return {"L": [write_text(value) for value in values]}


def _elected_snapshot_attributes(
    frontier: ServingFrontierVector, candidate_digest: str, run_ids: tuple[str, ...]
) -> dict[str, AttributeValueTypeDef]:
    return {
        "elected_frontier_revisions": _list_int(frontier.revisions),
        "elected_frontier_visible_counts": _list_int(frontier.visible_counts),
        "elected_frontier_eligible_counts": _list_int(frontier.eligible_counts),
        "elected_frontier_digest": write_text(frontier.digest),
        "elected_candidate_digest": write_text(candidate_digest),
        "elected_run_ids": _list_text(run_ids),
    }


def _text(item: Item, name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return None if value is None else str(value)


def _int(item: Item, name: str) -> int | None:
    value = item.get(name, {}).get("N")
    return None if value is None else int(str(value))


def _req_text(item: Item, name: str) -> str:
    value = _text(item, name)
    if value is None:
        raise GraphStoreUnavailableError(f"a release item is missing {name}.")
    return value


def _req_int(item: Item, name: str) -> int:
    value = _int(item, name)
    if value is None:
        raise GraphStoreUnavailableError(f"a release item is missing {name}.")
    return value


def _pin_refusal(item: Item) -> PinRefusal | None:
    """Read a pin item's optional refusal (ADR-142); an absent attribute is None."""
    reason, attempts = _text(item, "pin_refusal"), _int(item, "pin_refusal_attempts")
    if reason is None and attempts is None:
        return None
    if reason is None or attempts is None:
        raise DurableBakeSourceManifestInvalidError(
            "a pin refusal is missing its reason or attempts"
        )
    return PinRefusal(attempts, reason)


def _number_list(item: Item, name: str) -> tuple[int, ...]:
    return tuple(int(str(value.get("N", "0"))) for value in item.get(name, {}).get("L", []))


def _text_list(item: Item, name: str) -> tuple[str, ...]:
    return tuple(str(value.get("S", "")) for value in item.get(name, {}).get("L", []))


def _unavailable_list(unavailable: tuple[tuple[str, str], ...]) -> AttributeValueTypeDef:
    """S6 §2.8: `(artifact_key, reason)` pairs as a list of two-string lists, in order."""
    return {"L": [{"L": [write_text(key), write_text(reason)]} for key, reason in unavailable]}


def _read_unavailable(item: Item) -> tuple[tuple[str, str], ...]:
    """Read `_unavailable_list`'s shape back; an absent attribute is an empty list."""
    return _read_pair_list(item, "unavailable")


def _read_pair_list(item: Item, name: str) -> tuple[tuple[str, str], ...]:
    """Read one stored list of two-string lists."""
    pairs: list[tuple[str, str]] = []
    for member in item.get(name, {}).get("L", []):
        values = member.get("L", [])
        if len(values) != 2:  # noqa: PLR2004 -- exactly one key and one value
            raise GraphStoreUnavailableError(f"a {name} entry is not a string pair.")
        pairs.append((str(values[0].get("S", "")), str(values[1].get("S", ""))))
    return tuple(pairs)


def serving_binding_key(grant_id: str) -> Item:
    """Return the one `SERVING#<grant_id>/BINDING` key, shared with presentation (S6 §2.9)."""
    return _key(f"SERVING#{grant_id}", "BINDING")


def serving_binding_from_item(grant_id: str, item: Item) -> ServingBinding:
    """Parse one stored serving binding; a missing field is a store failure."""
    roots = item.get("contributor_roots", {}).get("L", [])
    return ServingBinding(
        binding_id=_req_text(item, "binding_id"),
        grant_id=grant_id,
        conflict_unit=_req_text(item, "conflict_unit"),
        served_revision=datetime.fromisoformat(_req_text(item, "served_revision")),
        artifact_revision=_text(item, "artifact_revision"),
        contributor_roots=tuple(str(root.get("S", "")) for root in roots),
        bound_at=datetime.fromisoformat(_req_text(item, "bound_at")),
        unavailable=_read_unavailable(item),
        root_flow_id=_text(item, "root_flow_id"),
        authority_epoch=_int(item, "authority_epoch"),
        domain=_text(item, "domain"),
        cohort_revision=_text(item, "cohort_revision"),
        serving_input_revision=_text(item, "serving_input_revision"),
        plan_digest=_text(item, "plan_digest"),
        binding_kind=(
            None
            if _text(item, "binding_kind") is None
            else BindingKind(cast("str", _text(item, "binding_kind")))
        ),
        direct_binding_id=_text(item, "direct_binding_id"),
        winner_run_id=_text(item, "winner_run_id"),
        execution_attempts=_int(item, "execution_attempts") or 0,
        stale=bool(item.get("stale", {}).get("BOOL", False)),
    )


def _ordinal_sk(prefix: str, ordinal: int) -> str:
    return f"{prefix}#{ordinal:010d}"


def _order_key(domain: str) -> Item:
    return _key(f"RELEASE_ORDER#{domain}", "HEAD")


def _run_key(run_id: str) -> Item:
    return _key(f"RUN#{run_id}", "RUN")


def _head_condition(signal: str, epoch: int) -> tuple[str, dict[str, AttributeValueTypeDef], str]:
    """`r >= H(U)` on the graph head: absent, or a head epoch at or below `r`."""
    return (
        "attribute_not_exists(pk) OR head_epoch < :r OR head_epoch = :r",
        _values(r=write_int(epoch)),
        signal,
    )


def _active_check(order: ReleaseOrderHead, run_id: str, signal: str) -> Member:
    if order.active_ordinal is None:
        raise GraphStoreUnavailableError("the release-order head holds no active ordinal.")
    return check(
        MemberSpec(
            "",
            _order_key(order.domain),
            signal,
            "active_ordinal = :ord AND active_run_id = :run",
            _values(ord=write_int(order.active_ordinal), run=write_text(run_id)),
        )
    )


def _order_release(order: ReleaseOrderHead, run_id: str, grant_id: str | None) -> Member:
    """Return the domain head to idle; only the run holding the active ordinal may."""
    if order.active_ordinal is None:
        raise GraphStoreUnavailableError("the release-order head holds no active ordinal.")
    values = _values(ord=write_int(order.active_ordinal), run=write_text(run_id), one=write_int(1))
    expression = "REMOVE active_ordinal, active_run_id SET revision = revision + :one"
    if grant_id is not None:
        values[":grant"] = write_text(grant_id)
        expression += ", last_grant_id = :grant"
    return update(
        MemberSpec(
            "",
            _order_key(order.domain),
            RELEASE_ORDER_BUSY,
            "active_ordinal = :ord AND active_run_id = :run",
            values,
        ),
        expression,
    )


def _not_blocked(domain: str) -> Member:
    return check(
        MemberSpec(
            "",
            _key(f"BLOCK#{domain}", "BLOCK"),
            CONFLICT_DOMAIN_BLOCKED,
            "attribute_not_exists(pk)",
        )
    )


def _wake(run: RunRecord, at: str) -> Member:
    """Release point 2 wakes `PROMOTE#<U>` like release point 1 (`ADD wake_seq 1`, r5)."""
    return update(
        MemberSpec(
            "",
            _key(due_shard(run.conflict_unit), f"PROMOTE#{run.conflict_unit}"),
            PROMOTE_WAKE_CHANGED,
            None,
            _values(
                one=write_int(1),
                at=write_text(at),
                held=write_bool(False),
                unit=write_text(run.conflict_unit),
                kind=write_text("promote"),
                epoch=write_int(run.authority_epoch),
            ),
        ),
        "ADD wake_seq :one SET woken_at = :at, held = :held, conflict_unit = :unit, "
        "item_kind = :kind, authority_epoch = :epoch",
    )


def _slot_release(run: RunRecord) -> Member:
    return update(
        MemberSpec(
            "",
            _key(f"SLOT#{run.conflict_unit}", "SLOT"),
            SLOT_NOT_OWNED,
            "active_run_id = :run",
            _values(run=write_text(run.run_id), one=write_int(1)),
        ),
        "REMOVE active_run_id SET revision = revision + :one",
    )


def _node_settle(node: NodeRecord, to: NodeState, at: str) -> Member:
    return update(
        MemberSpec(
            "",
            _key(f"RUN#{node.run_id}", f"NODE#{node.name}"),
            NODE_STATE_CHANGED,
            "#state = :from AND revision = :rev",
            _values(
                to=write_text(to.value),
                one=write_int(1),
                at=write_text(at),
                rev=write_int(node.revision),
                **{"from": write_text(node.state.value)},
            ),
        ),
        "SET #state = :to, revision = revision + :one, settled_at = :at",
    )


def _post_grant_members(run: RunRecord, spec: NodeSpec, at: str) -> list[Member]:
    """Write one POST_GRANT node READY with its token, domain lock and outbox row.

    Only grant apply writes these (design.md, "One claim authority"): the node does not exist
    before the grant, so its body can bind the exact grant and served revision. S6 §2.2: only a
    node whose one predecessor is `grant_apply` (`publish`) is armed here, with `armed_at`; the
    others are written PENDING and the arm pass arms them in the serving order.
    """
    envelope = graph_envelope(run.run_id, spec)
    epoch = write_int(run.authority_epoch)
    generation = write_int(run.dispatch_generation)
    shard = due_shard(run.conflict_unit)
    return [
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(f"RUN#{run.run_id}", f"NODE#{spec.name}"),
                "item_kind": write_text("node"),
                "authority_epoch": epoch,
                "root_flow_id": write_text(run.root_flow_id),
                "run_id": write_text(run.run_id),
                "node": write_text(spec.name),
                "node_id": write_text(envelope.node_id),
                "state": write_text(NodeState.READY.value),
                "revision": write_int(1),
                "phase": write_text(spec.phase.value),
                "after": {"L": [write_text(name) for name in spec.after]},
                "lock_key": write_text(spec.lock_key),
                "group_id": write_text(spec.group_id),
                "body": write_text(envelope.body),
                "envelope_digest": write_text(envelope.digest),
                "measured_bytes": write_int(provider_accounted_bytes(envelope.body, {})),
                "dispatch_id": write_text(envelope.dispatch_id),
                "armed_generation": generation,
                "armed_at": write_text(at),
                "put_at": write_text(at),
            },
        ),
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(f"TOKEN#{envelope.node_id}", "TOKEN"),
                "item_kind": write_text("token"),
                "authority_epoch": epoch,
                "run_id": write_text(run.run_id),
                "node_id": write_text(envelope.node_id),
            },
        ),
        put(
            MemberSpec("", {}, DOMAIN_LOCK_HELD, "attribute_not_exists(pk)"),
            {
                **_key(f"LOCK#{spec.lock_key}", "LOCK"),
                "item_kind": write_text("lock"),
                "authority_epoch": epoch,
                "holder_node_id": write_text(envelope.node_id),
                "run_id": write_text(run.run_id),
            },
        ),
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(shard, f"OUTBOX#{envelope.node_id}"),
                "item_kind": write_text("outbox"),
                "authority_epoch": epoch,
                "run_id": write_text(run.run_id),
                "node": write_text(spec.name),
                "generation": generation,
                "revision": write_int(1),
                "state": write_text(_OPEN),
                "due_at": write_text(at),
                **root_clock_attribute(run.root_clock_started_at),
            },
        ),
    ]


def _grant_item(grant: GrantRecord) -> Item:
    item: Item = {
        **_key(f"GRANT#{grant.grant_id}", "GRANT"),
        "item_kind": write_text("grant"),
        "authority_epoch": write_int(1),
        "grant_id": write_text(grant.grant_id),
        "run_id": write_text(grant.run_id),
        "domain": write_text(grant.domain),
        "conflict_unit": write_text(grant.conflict_unit),
        "active_ordinal": write_int(grant.ordinal),
        "set_id": write_text(grant.set_id),
        "proof_digest": write_text(grant.proof_digest),
        "retrieved_at": write_text(grant.retrieved_at),
        "lineage_seq": write_int(grant.lineage_sequence),
        "recorded_at": write_text(grant.recorded_at),
    }
    if grant.head_at_grant is not None:
        item["head_at_grant"] = write_text(grant.head_at_grant)
    return item


def _lineage_resolve(old: Item | None) -> str:
    """Map a failed candidate on the lineage head to its own refusal (BF-4)."""
    if old is not None and "failed_candidate" in old:
        return LINEAGE_REPAIR_UNSUPPORTED
    return RUN_STATE_CHANGED


def _serving_binding_item(binding: ServingBinding) -> Item:
    item: Item = {
        **serving_binding_key(binding.grant_id),
        "item_kind": write_text("serving_binding"),
        "binding_id": write_text(binding.binding_id),
        "conflict_unit": write_text(binding.conflict_unit),
        "served_revision": write_text(binding.served_revision.isoformat()),
        "contributor_roots": {"L": [write_text(root) for root in binding.contributor_roots]},
        "bound_at": write_text(binding.bound_at.isoformat()),
        "unavailable": _unavailable_list(binding.unavailable),
        "execution_attempts": write_int(binding.execution_attempts),
        "stale": write_bool(binding.stale),
    }
    optional_text = {
        "artifact_revision": binding.artifact_revision,
        "root_flow_id": binding.root_flow_id,
        "domain": binding.domain,
        "cohort_revision": binding.cohort_revision,
        "serving_input_revision": binding.serving_input_revision,
        "plan_digest": binding.plan_digest,
        "binding_kind": None if binding.binding_kind is None else binding.binding_kind.value,
        "direct_binding_id": binding.direct_binding_id,
        "winner_run_id": binding.winner_run_id,
    }
    item.update(
        {name: write_text(value) for name, value in optional_text.items() if value is not None}
    )
    if binding.authority_epoch is not None:
        item["authority_epoch"] = write_int(binding.authority_epoch)
    return item


def _contribution_item(contribution: CohortContribution) -> Item:
    """Return one immutable `COHORT#<grant>/CONTRIBUTION#<member>` item."""
    item: Item = {
        **_key(f"COHORT#{contribution.grant_id}", f"CONTRIBUTION#{contribution.member.value}"),
        "item_kind": write_text("cohort_contribution"),
        "outcome": write_text(contribution.outcome.value),
        "settled_at": write_text(contribution.settled_at.isoformat()),
        "root_flow_id": write_text(contribution.root_flow_id),
    }
    if contribution.artifact_revision is not None:
        item["artifact_revision"] = write_text(contribution.artifact_revision)
    if contribution.unavailable:
        item["unavailable"] = _unavailable_list(contribution.unavailable)
    if contribution.input_effect_roster_digest is not None:
        item["input_effect_roster_digest"] = write_text(contribution.input_effect_roster_digest)
        item["request_roster"] = {
            "L": [
                {"L": [write_text(key), write_text(disposition)]}
                for key, disposition in contribution.request_roster
            ]
        }
    return item


def _precompute_check(grant_id: str, precompute: CohortContribution) -> Member:
    """Condition the final transaction on the exact stored precompute member.

    ADR-140 (Z3): a stale member may carry no readiness digest; `record_contribution` then omits
    the attribute, so the condition requires its absence rather than an empty string.
    """
    digest = precompute.input_effect_roster_digest
    values = _values(
        outcome=write_text(precompute.outcome.value),
        settled=write_text(precompute.settled_at.isoformat()),
    )
    condition = "outcome = :outcome AND settled_at = :settled AND "
    if digest is None:
        condition += "attribute_not_exists(input_effect_roster_digest)"
    else:
        condition += "input_effect_roster_digest = :input_digest"
        values[":input_digest"] = write_text(digest)
    return check(
        MemberSpec(
            "",
            _key(f"COHORT#{grant_id}", "CONTRIBUTION#precompute"),
            NODE_STATE_CHANGED,
            condition,
            values,
        )
    )


def build_publication_flip_participant(  # noqa: PLR0913 -- exact flip authority inputs
    store: DynamoGraphDispatchStore,
    authority: ServingPublicationAuthority,
    *,
    root_flow_id: str,
    target_digest: str,
    flipped_at: str,
) -> tuple[tuple[TransactWriteItemTypeDef, ...], Item, Item]:
    """Return the exact graph participant for the semantic store's one cross-table flip."""
    return store.build_publication_flip_participant(
        authority,
        root_flow_id=root_flow_id,
        target_digest=target_digest,
        flipped_at=flipped_at,
    )


class DynamoGraphReleaseStore(DynamoGraphDispatchStore):
    """The release authority; it reads runs and nodes through the dispatch adapter it extends."""

    # --- reads ----------------------------------------------------------------------------------

    def read_release_order(self, domain: str) -> ReleaseOrderHead:
        """Read the domain's release-order head; absent reads revision 0, idle."""
        item = self._get(_order_key(domain))
        if item is None:
            return ReleaseOrderHead(domain, 0, 1)
        return ReleaseOrderHead(
            domain=domain,
            revision=_req_int(item, "revision"),
            next_ordinal=_req_int(item, "next_ordinal"),
            active_ordinal=_int(item, "active_ordinal"),
            active_run_id=_text(item, "active_run_id"),
        )

    def read_block(self, domain: str) -> BlockRecord | None:
        """Read the domain's block record."""
        item = self._get(_key(f"BLOCK#{domain}", "BLOCK"))
        if item is None:
            return None
        return BlockRecord(
            domain=domain,
            reason=_req_text(item, "block_reason"),
            run_id=_req_text(item, "run_id"),
            grant_id=_text(item, "grant_id"),
            blocked_at=_req_text(item, "blocked_at"),
        )

    def read_graph_head(self, conflict_unit: str) -> tuple[datetime | None, int | None]:
        """Read `HEAD#<U>`: its retrieval instant and its epoch seconds."""
        item = self._get(_key(f"HEAD#{conflict_unit}", "HEAD"))
        if item is None:
            return None, None
        instant = _text(item, "retrieved_at")
        parsed = None if instant is None else datetime.fromisoformat(instant)
        epoch = _int(item, "head_epoch")
        if epoch is None and parsed is not None:
            epoch = head_epoch(parsed)
        return parsed, epoch

    def read_lineage(self, conflict_unit: str) -> LineageHead:
        """Read `LINEAGE#<U>/HEAD`; absent reads sequence 0 with no tail."""
        item = self._get(_key(f"LINEAGE#{conflict_unit}", "HEAD"))
        if item is None:
            return LineageHead(0, None)
        return LineageHead(
            sequence=_req_int(item, "lineage_seq"),
            tail_grant_id=_text(item, "tail_grant_id"),
            failed="failed_candidate" in item,
        )

    def read_grant(self, grant_id: str) -> GrantRecord | None:
        """Read one grant."""
        item = self._get(_key(f"GRANT#{grant_id}", "GRANT"))
        if item is None:
            return None
        return GrantRecord(
            grant_id=grant_id,
            run_id=_req_text(item, "run_id"),
            domain=_req_text(item, "domain"),
            conflict_unit=_req_text(item, "conflict_unit"),
            ordinal=_req_int(item, "active_ordinal"),
            set_id=_req_text(item, "set_id"),
            proof_digest=_req_text(item, "proof_digest"),
            retrieved_at=_req_text(item, "retrieved_at"),
            head_at_grant=_text(item, "head_at_grant"),
            lineage_sequence=_req_int(item, "lineage_seq"),
            recorded_at=_req_text(item, "recorded_at"),
        )

    def read_reserved_targets(self, reservation: ReservationSet) -> tuple[str, ...]:
        """Return the set's targets that hold this set's reservation."""
        held: list[str] = []
        for target in reservation.targets:
            item = self._get(
                _key(f"TARGET#{target}", _ordinal_sk("RESERVATION", reservation.ordinal))
            )
            if item is not None and _text(item, "set_id") == reservation.set_id:
                held.append(target)
        return tuple(held)

    def proof_digest(self, reservation: ReservationSet) -> str | None:
        """Return the set's proof digest, or None."""
        item = self._get(_key(f"RESERVATION_SET#{reservation.set_id}", "PROOF"))
        return None if item is None else _text(item, "set_digest")

    # --- ordinal and reservations (BF-1) --------------------------------------------------------

    def acquire_ordinal(
        self, order: ReleaseOrderHead, run: RunRecord, reservation: ReservationSet
    ) -> None:
        """CAS the idle head to this run's ordinal; create its intent and reservation set."""
        ordinal = reservation.ordinal
        if ordinal != order.next_ordinal:
            raise GraphStoreUnavailableError("the reservation set names another ordinal.")
        if order.revision == 0:
            condition, values = "attribute_not_exists(pk)", _values()
        else:
            condition = "revision = :rev AND attribute_not_exists(active_run_id)"
            values = _values(rev=write_int(order.revision))
        values.update(
            _values(
                ord=write_int(ordinal),
                run=write_text(run.run_id),
                next=write_int(ordinal + 1),
                nextrev=write_int(order.revision + 1),
                kind=write_text("release_order"),
            )
        )
        roster: AttributeValueTypeDef = {
            "L": [write_text(target) for target in reservation.targets]
        }
        self._commit(
            [
                update(
                    MemberSpec("", _order_key(order.domain), RELEASE_ORDER_BUSY, condition, values),
                    "SET active_ordinal = :ord, active_run_id = :run, next_ordinal = :next, "
                    "revision = :nextrev, item_kind = :kind",
                ),
                put(
                    MemberSpec("", {}, RELEASE_ORDER_BUSY, "attribute_not_exists(pk)"),
                    {
                        **_key(f"RELEASE_ORDER#{order.domain}", _ordinal_sk("INTENT", ordinal)),
                        "item_kind": write_text("release_intent"),
                        "run_id": write_text(run.run_id),
                        "conflict_unit": write_text(run.conflict_unit),
                        "set_id": write_text(reservation.set_id),
                    },
                ),
                put(
                    MemberSpec("", {}, RELEASE_ORDER_BUSY, "attribute_not_exists(pk)"),
                    {
                        **_key(f"RESERVATION_SET#{reservation.set_id}", "SET"),
                        "item_kind": write_text("reservation_set"),
                        "run_id": write_text(run.run_id),
                        "domain": write_text(order.domain),
                        "active_ordinal": write_int(ordinal),
                        "targets": roster,
                        "target_count": write_int(len(reservation.targets)),
                        "set_digest": write_text(reservation.digest),
                    },
                ),
                check(
                    MemberSpec(
                        "",
                        _run_key(run.run_id),
                        RUN_STATE_CHANGED,
                        "#state = :open AND attribute_not_exists(grant_id)",
                        _values(open=write_text(RunState.OPEN.value)),
                    )
                ),
                _not_blocked(order.domain),
            ]
        )

    def reserve_target(self, reservation: ReservationSet, target: str) -> None:
        """Append this ordinal's reservation to the target's tail, strictly increasing."""
        order = ReleaseOrderHead(
            reservation.domain, 0, reservation.ordinal + 1, reservation.ordinal, reservation.run_id
        )
        self._commit(
            [
                update(
                    MemberSpec(
                        "",
                        _key(f"TARGET#{target}", "TAIL"),
                        RELEASE_ORDER_BUSY,
                        "attribute_not_exists(pk) OR tail_ordinal < :ord",
                        _values(
                            ord=write_int(reservation.ordinal), set=write_text(reservation.set_id)
                        ),
                    ),
                    "SET tail_ordinal = :ord, tail_set = :set",
                ),
                put(
                    MemberSpec("", {}, RELEASE_ORDER_BUSY, "attribute_not_exists(pk)"),
                    {
                        **_key(f"TARGET#{target}", _ordinal_sk("RESERVATION", reservation.ordinal)),
                        "item_kind": write_text("target_reservation"),
                        "set_id": write_text(reservation.set_id),
                        "run_id": write_text(reservation.run_id),
                    },
                ),
                _active_check(order, reservation.run_id, RELEASE_ORDER_BUSY),
            ]
        )

    def seal_proof(self, reservation: ReservationSet) -> None:
        """Write `TARGET_RESERVATION_PROOF` after ConditionChecking every reservation."""
        members: list[Member] = [
            check(
                MemberSpec(
                    "",
                    _key(f"TARGET#{target}", _ordinal_sk("RESERVATION", reservation.ordinal)),
                    RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
                    "set_id = :set",
                    _values(set=write_text(reservation.set_id)),
                )
            )
            for target in reservation.targets
        ]
        members.append(
            put(
                MemberSpec(
                    "", {}, RELEASE_GRANT_RESERVATION_PROOF_REQUIRED, "attribute_not_exists(pk)"
                ),
                {
                    **_key(f"RESERVATION_SET#{reservation.set_id}", "PROOF"),
                    "item_kind": write_text("reservation_proof"),
                    "target_count": write_int(len(reservation.targets)),
                    "set_digest": write_text(reservation.digest),
                },
            )
        )
        self._commit(members)

    # --- the grant (BF-1, V-28, V-29) -----------------------------------------------------------

    def issue_grant(self, commit: GrantCommit) -> None:
        """Create the grant, mark the run, check the head, seal lineage and write `APPLY#`."""
        run, grant, reservation = commit.run, commit.grant, commit.reservation
        shard = due_shard(run.conflict_unit)
        r_epoch = head_epoch(datetime.fromisoformat(run.retrieved_at))
        head_condition, head_values, head_signal = _head_condition(
            INPUT_REVISION_SUPERSEDED, r_epoch
        )
        step = commit.lineage
        if commit.lineage_head.sequence == 0:
            lineage_condition = "attribute_not_exists(pk)"
            lineage_values = _values(
                next=write_int(step.sequence), grant=write_text(grant.grant_id)
            )
        else:
            lineage_condition = "lineage_seq = :seq AND attribute_not_exists(failed_candidate)"
            lineage_values = _values(
                seq=write_int(commit.lineage_head.sequence),
                next=write_int(step.sequence),
                grant=write_text(grant.grant_id),
            )
        step_item: Item = {
            **_key(f"LINEAGE#{run.conflict_unit}", _ordinal_sk("STEP", step.sequence)),
            "item_kind": write_text("lineage_step"),
            "candidate_grant_id": write_text(step.candidate_grant_id),
            "run_id": write_text(run.run_id),
            "retrieved_at": write_text(run.retrieved_at),
        }
        if step.predecessor_grant_id is not None:
            step_item["predecessor_grant_id"] = write_text(step.predecessor_grant_id)
        # The lineage member follows the run member, so a failed candidate is reported as its
        # own refusal (BF-4) rather than as the ordinal or block it left behind.
        lineage_member = update(
            MemberSpec(
                "",
                _key(f"LINEAGE#{run.conflict_unit}", "HEAD"),
                RUN_STATE_CHANGED,
                lineage_condition,
                lineage_values,
                _lineage_resolve,
            ),
            "SET lineage_seq = :next, tail_grant_id = :grant",
        )
        self._commit(
            [
                update(
                    MemberSpec(
                        "",
                        _run_key(run.run_id),
                        RUN_STATE_CHANGED,
                        "#state = :open AND attribute_not_exists(grant_id) AND revision = :rev",
                        _values(
                            open=write_text(RunState.OPEN.value),
                            rev=write_int(run.revision),
                            grant=write_text(grant.grant_id),
                            one=write_int(1),
                            at=write_text(grant.recorded_at),
                            ordinal=write_int(grant.ordinal),
                        ),
                    ),
                    # S6 (AGG4-16): the grant's domain ordinal rides on RUN#, so `publish` stamps
                    # it and the flip conditions on it.
                    "SET grant_id = :grant, granted_at = :at, grant_ordinal = :ordinal, "
                    "revision = revision + :one",
                ),
                lineage_member,
                _active_check(commit.order, run.run_id, RELEASE_GRANT_RESERVATION_PROOF_REQUIRED),
                check(
                    MemberSpec(
                        "",
                        _key(f"RESERVATION_SET#{reservation.set_id}", "PROOF"),
                        RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
                        "set_digest = :digest AND target_count = :count",
                        _values(
                            digest=write_text(reservation.digest),
                            count=write_int(len(reservation.targets)),
                        ),
                    )
                ),
                check(
                    MemberSpec(
                        "",
                        _key(f"HEAD#{run.conflict_unit}", "HEAD"),
                        head_signal,
                        head_condition,
                        head_values,
                    )
                ),
                _not_blocked(grant.domain),
                put(
                    MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                    _grant_item(grant),
                ),
                put(MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"), step_item),
                put(
                    MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                    {
                        **_key(shard, f"APPLY#{run.run_id}"),
                        "item_kind": write_text("apply"),
                        "authority_epoch": write_int(run.authority_epoch),
                        "run_id": write_text(run.run_id),
                        "grant_id": write_text(grant.grant_id),
                        "due_at": write_text(grant.recorded_at),
                        **root_clock_attribute(run.root_clock_started_at),
                    },
                ),
                delete(MemberSpec("", _key(shard, f"RELEASE#{run.run_id}"), RUN_STATE_CHANGED)),
            ]
        )

    def refuse_superseded(self, refusal: SupersededGrant) -> None:
        """V-28: FENCING `older_than_current` (release point 1) and the ordinal returned."""
        run = refusal.run
        members = build_fence_run(
            FenceRunRequest(run, FenceReason.OLDER_THAN_CURRENT, refusal.recorded_at)
        )
        if refusal.order is not None:
            members.append(_order_release(refusal.order, run.run_id, None))
        members.append(
            delete(
                MemberSpec(
                    "",
                    _key(due_shard(run.conflict_unit), f"RELEASE#{run.run_id}"),
                    RUN_STATE_CHANGED,
                )
            )
        )
        self._commit(members)

    def retire_release(self, retired: RetiredRelease) -> None:
        """Return the ordinal of a run that left OPEN ungranted; retire a terminal run's trigger.

        The run's state and revision are ConditionChecked as read, so a resume or a grant that
        commits first makes this transaction lose and the caller decide again.
        """
        run = retired.run
        members: list[Member] = [
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :state AND revision = :rev AND attribute_not_exists(grant_id)",
                    _values(state=write_text(run.state.value), rev=write_int(run.revision)),
                )
            )
        ]
        if retired.order is not None:
            members.append(_order_release(retired.order, run.run_id, None))
        if retired.retire_trigger:
            members.append(
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(run.conflict_unit), f"RELEASE#{run.run_id}"),
                        RUN_STATE_CHANGED,
                    )
                )
            )
        if len(members) > 1:
            self._commit(members)

    # --- apply (BF-8, release point 2) ----------------------------------------------------------

    def _apply_grant_members(self, commit: ApplyCommit) -> list[Member]:
        """Build the existing grant-apply members for composition with serving bootstrap."""
        run, grant, at = commit.run, commit.grant, commit.recorded_at
        r = datetime.fromisoformat(run.retrieved_at)
        head_condition, head_values, head_signal = _head_condition(
            INPUT_REVISION_SUPERSEDED, head_epoch(r)
        )
        head_values.update(
            _values(
                instant=write_text(run.retrieved_at),
                grant=write_text(grant.grant_id),
                run=write_text(run.run_id),
                kind=write_text("head"),
            )
        )
        members: list[Member] = [
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :open AND grant_id = :grant",
                    _values(
                        open=write_text(RunState.OPEN.value),
                        granted=write_text(RunState.GRANTED.value),
                        grant=write_text(grant.grant_id),
                        one=write_int(1),
                        at=write_text(at),
                    ),
                ),
                "SET #state = :granted, applied_at = :at, revision = revision + :one",
            ),
            check(
                MemberSpec(
                    "",
                    _key(f"GRANT#{grant.grant_id}", "GRANT"),
                    RUN_STATE_CHANGED,
                    "run_id = :run",
                    _values(run=write_text(run.run_id)),
                )
            ),
            _slot_release(run),
            _wake(run, at),
            update(
                MemberSpec(
                    "",
                    _key(f"HEAD#{run.conflict_unit}", "HEAD"),
                    head_signal,
                    head_condition,
                    head_values,
                ),
                "SET retrieved_at = :instant, head_epoch = :r, grant_id = :grant, "
                "run_id = :run, item_kind = :kind",
            ),
            _order_release(commit.order, run.run_id, grant.grant_id),
            delete(
                MemberSpec(
                    "",
                    _key(due_shard(run.conflict_unit), f"APPLY#{run.run_id}"),
                    RUN_STATE_CHANGED,
                    "attribute_exists(pk)",
                )
            ),
        ]
        if commit.grant_apply is not None:
            members.append(_node_settle(commit.grant_apply, NodeState.SUCCEEDED, at))
        # S6 §2.2: arm only the specs whose one predecessor is `grant_apply` (settled above);
        # write the rest PENDING, and put `COMPLETE#`, the run's completion obligation and its
        # place in the domain's serving order (§2.1). 8 + 4 + 1 + 1 + 1 = 15 members.
        armed = tuple(spec for spec in commit.post_grant if spec.after == (GRANT_APPLY_NODE,))
        pending = tuple(spec for spec in commit.post_grant if spec not in armed)
        for spec in armed:
            members.extend(_post_grant_members(run, spec, at))
        successors = build_successors(SuccessorRequest(run, pending, None, False, at))
        waiting_state = str(BAKE_V3["waiting_input_state"])
        for member in successors:
            put_body = cast("dict[str, Any]", member.item).get("Put")
            item = None if put_body is None else put_body.get("Item")
            if item is not None and item.get("sk") == write_text("NODE#precompute"):
                item["state"] = write_text(waiting_state)
        members.extend(successors)
        members.append(
            put(
                MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                due_obligation_item(
                    run,
                    "complete",
                    at,
                    domain=write_text(grant.domain),
                    grant_id=write_text(grant.grant_id),
                    grant_ordinal=write_int(grant.ordinal),
                ),
            )
        )
        return members

    def apply_grant(self, commit: ApplyCommit) -> None:
        """Compatibility wrapper; active release uses atomic serving bootstrap and apply."""
        self._commit(self._apply_grant_members(commit))

    def bootstrap_serving_domain_and_apply_grant(
        self, request: ServingDomainBootstrapRequest
    ) -> ServingBootstrapApplyResult:
        """Apply a grant while establishing only domain/head/frontier authority."""
        current = self.read_serving_domain(request.head.domain)
        run = self.read_run(request.apply.run.run_id)
        if (
            run is not None
            and run.state is RunState.GRANTED
            and current.head is not None
            and current.frontier is not None
        ):
            return ServingBootstrapApplyResult.REPLAY
        if (current.head is None) != (current.frontier is None):
            raise GraphStoreUnavailableError(_SERVING_FRONTIER_PARTIAL)
        members = self._apply_grant_members(request.apply)
        for member in members:
            put_body = cast("dict[str, Any]", member.item).get("Put")
            item = None if put_body is None else put_body.get("Item")
            if item is not None and str(item.get("sk", {}).get("S", "")).startswith("COMPLETE#"):
                item.update(serving_authority_identity_attributes(request.identity))
        if current.head is None:
            members.append(
                put(
                    MemberSpec("", {}, _SERVING_HEAD_CHANGED, "attribute_not_exists(pk)"),
                    {
                        **_serving_head_key(request.head.domain),
                        "item_kind": write_text("serving_domain"),
                        "state": write_text("IDLE"),
                        "version": write_int(0),
                    },
                )
            )
            for shard in range(_SERVING_SHARDS):
                members.append(
                    put(
                        MemberSpec("", {}, _SERVING_FRONTIER_CHANGED, "attribute_not_exists(pk)"),
                        {
                            **_serving_frontier_key(request.head.domain, shard),
                            "item_kind": write_text("serving_frontier"),
                            "revision": write_int(0),
                            "visible_count": write_int(0),
                            "eligible_count": write_int(0),
                        },
                    )
                )
        else:
            members.append(
                check(
                    MemberSpec(
                        "",
                        _serving_head_key(request.head.domain),
                        _SERVING_HEAD_CHANGED,
                        "version = :version AND #state = :state",
                        _values(
                            version=write_int(current.head.version),
                            state=write_text(current.head.state),
                        ),
                    )
                )
            )
            if current.frontier is None:
                raise GraphStoreUnavailableError(_SERVING_FRONTIER_PARTIAL)
            members.extend(self._frontier_checks(request.head.domain, current.frontier))
        self._commit(members)
        return ServingBootstrapApplyResult.APPLIED

    def _frontier_checks(self, domain: str, frontier: ServingFrontierVector) -> list[Member]:
        return [
            check(
                MemberSpec(
                    "",
                    _serving_frontier_key(domain, shard),
                    _SERVING_FRONTIER_CHANGED,
                    "revision = :revision AND visible_count = :visible "
                    "AND eligible_count = :eligible",
                    _values(
                        revision=write_int(frontier.revisions[shard]),
                        visible=write_int(frontier.visible_counts[shard]),
                        eligible=write_int(frontier.eligible_counts[shard]),
                    ),
                )
            )
            for shard in range(_SERVING_SHARDS)
        ]

    @staticmethod
    def _candidate_digest(candidates: tuple[ServingCandidate, ...]) -> str:
        from domain.operations.status import canonical_authority_digest  # noqa: PLC0415

        return canonical_authority_digest(
            [
                "graph-serving-candidates/v1",
                [
                    [
                        item.run_id,
                        item.grant_id,
                        item.grant_ordinal,
                        item.publication_effect_digest,
                    ]
                    for item in candidates
                ],
            ]
        )

    def activate_serving_winner(
        self, request: ActivateServingWinnerRequest
    ) -> ServingActivationResult:
        """Activate one deterministic winner with an immutable elected snapshot."""
        run_ids = tuple(candidate.run_id for candidate in request.candidates)
        digest = self._candidate_digest(request.candidates)
        head_item = {
            **_elected_snapshot_attributes(request.frontier, digest, run_ids),
            "state": write_text("ACTIVE"),
            "cohort_revision": write_text(request.cohort_revision),
            "serving_input_revision": write_text(request.serving_input_revision),
            "winner_run_id": write_text(request.winner.run_id),
            "activated_at": write_text(request.recorded_at),
        }
        values = _values(
            version=write_int(request.head.version),
            idle=write_text("IDLE"),
            one=write_int(1),
            **head_item,
        )
        expression = "SET #state = :state, version = version + :one, " + ", ".join(
            f"{name} = :{name}" for name in head_item if name != "state"
        )
        run = self.read_run(request.winner.run_id)
        bake = self.read_node(request.winner.run_id, "bake")
        if run is None or bake is None:
            raise GraphStoreUnavailableError("the selected winner is incomplete.")
        due = due_shard(run.conflict_unit)
        selected = [
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND grant_id = :grant AND grant_ordinal = :ordinal",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        grant=write_text(request.winner.grant_id),
                        ordinal=write_int(request.winner.grant_ordinal),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _key(due, f"COMPLETE#{run.run_id}"),
                    RUN_STATE_CHANGED,
                    "attribute_exists(pk)",
                )
            ),
            check(
                MemberSpec(
                    "",
                    _serving_marker_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "attribute_exists(pk)",
                )
            ),
            update(
                MemberSpec(
                    "",
                    _key(f"RUN#{run.run_id}", "NODE#bake"),
                    NODE_STATE_CHANGED,
                    "#state = :pending AND revision = :revision",
                    _values(
                        pending=write_text(NodeState.PENDING.value),
                        ready=write_text(NodeState.READY.value),
                        revision=write_int(bake.revision),
                        one=write_int(1),
                        domain=write_text(request.head.domain),
                        cohort=write_text(request.cohort_revision),
                        input=write_text(request.serving_input_revision),
                        plan=write_text(request.plan_digest),
                        runs=_list_text(run_ids),
                        at=write_text(request.recorded_at),
                    ),
                ),
                "SET #state = :ready, revision = revision + :one, serving_domain = :domain, "
                "cohort_revision = :cohort, serving_input_revision = :input, "
                "serving_plan_digest = :plan, serving_run_ids = :runs, armed_at = :at",
            ),
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(f"TOKEN#{bake.node_id}", "TOKEN"),
                    "item_kind": write_text("token"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "run_id": write_text(run.run_id),
                    "node_id": write_text(bake.node_id),
                },
            ),
            put(
                MemberSpec("", {}, DOMAIN_LOCK_HELD, "attribute_not_exists(pk)"),
                {
                    **_key(f"LOCK#{bake.lock_key}", "LOCK"),
                    "item_kind": write_text("lock"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "holder_node_id": write_text(bake.node_id),
                    "run_id": write_text(run.run_id),
                },
            ),
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(due, f"OUTBOX#{bake.node_id}"),
                    "item_kind": write_text("outbox"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "run_id": write_text(run.run_id),
                    "node": write_text("bake"),
                    "generation": write_int(run.dispatch_generation),
                    "revision": write_int(1),
                    "state": write_text("OPEN"),
                    "due_at": write_text(request.recorded_at),
                    **root_clock_attribute(run.root_clock_started_at),
                },
            ),
            delete(MemberSpec("", _key(due, f"ARM#{run.run_id}"), RUN_STATE_CHANGED)),
        ]
        members = [
            *self._frontier_checks(request.head.domain, request.frontier),
            *selected,
            update(
                MemberSpec(
                    "",
                    _serving_head_key(request.head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :idle",
                    values,
                ),
                expression,
            ),
            put(
                MemberSpec("", {}, _SERVING_HEAD_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(due_shard(request.head.domain), f"COALESCE#{request.cohort_revision}"),
                    "item_kind": write_text("coalesce"),
                    "domain": write_text(request.head.domain),
                    "winner_run_id": write_text(request.winner.run_id),
                    "run_ids": _list_text(run_ids),
                    "plan_digest": write_text(request.plan_digest),
                    "due_at": write_text(request.recorded_at),
                    **root_clock_attribute(cohort_root_clock(request.candidates)),
                },
            ),
        ]
        self._commit(members)
        return ServingActivationResult(ServingMutationResult.COMMITTED)

    # --- serving authority, capacity, and binding cleanup -------------------------------------

    def read_serving_domain(self, domain: str) -> ServingDomainReadResult:
        """Strongly read the domain head and every cumulative live-frontier shard."""
        head_item = self._get(_serving_head_key(domain))
        shard_items = [
            self._get(_serving_frontier_key(domain, shard)) for shard in range(_SERVING_SHARDS)
        ]
        present = [item is not None for item in shard_items]
        if any(present) and not all(present):
            raise GraphStoreUnavailableError(_SERVING_FRONTIER_PARTIAL)
        head = None
        if head_item is not None:
            elected_frontier = None
            elected_revisions = _number_list(head_item, "elected_frontier_revisions")
            elected_visible = _number_list(head_item, "elected_frontier_visible_counts")
            elected_eligible = _number_list(head_item, "elected_frontier_eligible_counts")
            if elected_revisions or elected_visible or elected_eligible:
                elected_frontier = ServingFrontierVector(
                    elected_revisions, elected_visible, elected_eligible
                )
            head = ServingDomainHead(
                domain=domain,
                state=_req_text(head_item, "state"),
                version=_req_int(head_item, "version"),
                cohort_revision=_text(head_item, "cohort_revision"),
                serving_input_revision=_text(head_item, "serving_input_revision"),
                winner_run_id=_text(head_item, "winner_run_id"),
                direct_binding_id=_text(head_item, "direct_binding_id"),
                direct_grant_id=_text(head_item, "direct_grant_id"),
                elected_frontier=elected_frontier,
                elected_frontier_digest=_text(head_item, "elected_frontier_digest"),
                elected_candidate_digest=_text(head_item, "elected_candidate_digest"),
                elected_run_ids=_text_list(head_item, "elected_run_ids"),
            )
        frontier = None
        if all(present):
            complete = [cast("Item", item) for item in shard_items]
            frontier = ServingFrontierVector(
                revisions=tuple(_req_int(item, "revision") for item in complete),
                visible_counts=tuple(_req_int(item, "visible_count") for item in complete),
                eligible_counts=tuple(_req_int(item, "eligible_count") for item in complete),
            )
        if (head is None) != (frontier is None):
            raise GraphStoreUnavailableError(_SERVING_FRONTIER_PARTIAL)
        return ServingDomainReadResult(head, frontier)

    def read_serving_candidate(self, run_id: str) -> ServingCandidateRead | None:
        """Read one settled marker plus matching ARM/COMPLETE/run/node authority."""
        run = self.read_run(run_id)
        if run is None or run.grant_id is None or run.grant_ordinal is None:
            return None
        due = due_shard(run.conflict_unit)
        arm = self._get(_key(due, f"ARM#{run_id}"))
        complete = self._get(_key(due, f"COMPLETE#{run_id}"))
        marker = self._get(_serving_marker_key(run_id))
        if arm is None or complete is None or marker is None:
            return None
        if _text(marker, "state") != "SETTLED_ELIGIBLE":
            return None
        identity = serving_authority_identity_from_item(complete)
        for item in (arm, marker):
            try:
                require_serving_authority_identity(
                    identity, serving_authority_identity_from_item(item)
                )
            except ValueError:
                raise GraphStoreUnavailableError(
                    "a serving candidate has mismatched ARM/marker authority."
                ) from None
        publish = self.read_node(run_id, "publish")
        precompute = self.read_node(run_id, "precompute")
        bake = self.read_node(run_id, "bake")
        if publish is None or precompute is None or bake is None or publish.effect_digest is None:
            raise GraphStoreUnavailableError("a serving candidate has incomplete worker nodes.")
        if precompute.state is not NodeState.WAITING_INPUT or bake.state is not NodeState.PENDING:
            return None
        runtime_present = any(
            self.read_claim(node.node_id) is not None for node in (precompute, bake)
        )
        candidate = ServingCandidate(
            run_id=run_id,
            grant_id=run.grant_id,
            root_flow_id=run.root_flow_id,
            domain=identity.domain,
            grant_ordinal=run.grant_ordinal,
            publication_effect_digest=publish.effect_digest,
            run_state=run.state,
            arm_matches=True,
            complete_matches=True,
            publish_state=publish.state,
            precompute_state=precompute.state,
            bake_state=bake.state,
            runtime_state_present=runtime_present,
            binding_present=self.read_serving_binding(run.grant_id) is not None,
            root_clock_started_at=run.root_clock_started_at,
        )
        return ServingCandidateRead(candidate, identity, _req_text(marker, "target_digest"))

    def read_serving_execution(self, grant_id: str, run_id: str) -> ServingExecutionContext | None:
        """Read winner context stamped on its bake fan-out node by the atomic election."""
        run = self.read_run(run_id)
        if run is None or run.grant_id != grant_id:
            return None
        node = self.read_node(run_id, "bake")
        if (
            node is None
            or node.serving_domain is None
            or node.cohort_revision is None
            or node.serving_input_revision is None
            or node.serving_plan_digest is None
            or not node.serving_run_ids
        ):
            return None
        return ServingExecutionContext(
            head=ServingDomainHead(
                domain=node.serving_domain,
                state="ACTIVE",
                version=0,
                cohort_revision=node.cohort_revision,
                serving_input_revision=node.serving_input_revision,
                winner_run_id=run_id,
            ),
            run_ids=node.serving_run_ids,
            plan_digest=node.serving_plan_digest,
        )

    @staticmethod
    def _binding_member(  # noqa: PLR0913, PLR0917
        binding: ServingBinding,
        existing: ServingBinding | None,
        operation: str,
        candidate_run_id: str,
        identity: ServingAuthorityIdentity | None = None,
    ) -> Member:
        """Write the operation-owned terminal marker on one immutable binding."""
        kind = binding.binding_kind
        if kind is None:
            raise GraphStoreUnavailableError("a terminal serving binding must be typed.")
        values = _values(
            binding=write_text(binding.binding_id),
            kind=write_text(kind.value),
            operation=write_text(operation),
            run=write_text(candidate_run_id),
        )
        if existing is None:
            item = _serving_binding_item(binding)
            item.update(
                terminal_operation=write_text(operation),
                terminal_candidate_run_id=write_text(candidate_run_id),
            )
            if identity is not None:
                item.update(serving_authority_identity_attributes(identity))
            return put(MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"), item)
        expression = "SET terminal_operation = :operation, terminal_candidate_run_id = :run"
        if identity is not None:
            values.update(
                {
                    ":grant": write_text(identity.grant_id),
                    ":ordinal": write_int(identity.grant_ordinal),
                    ":domain": write_text(identity.domain),
                }
            )
            expression += (
                ", run_id = :run, grant_id = :grant, grant_ordinal = :ordinal, #domain = :domain"
            )
        return update(
            MemberSpec(
                "",
                serving_binding_key(binding.grant_id),
                RUN_STATE_CHANGED,
                "binding_id = :binding AND binding_kind = :kind "
                "AND attribute_not_exists(terminal_operation)",
                values,
            ),
            expression,
        )

    def _unexecuted_candidate_terminal_members(  # noqa: PLR0913, PLR0917
        self,
        candidate: ServingCandidate,
        binding: ServingBinding,
        state: NodeState,
        terminal_at: str,
        *,
        outcome_binding: ServingBinding | None = None,
    ) -> list[Member]:
        """Settle both unexecuted workers and their run at the caller-owned instant."""
        run = self.read_run(candidate.run_id)
        precompute = self.read_node(candidate.run_id, "precompute")
        bake = self.read_node(candidate.run_id, "bake")
        if run is None or precompute is None or bake is None:
            raise GraphStoreUnavailableError("an unexecuted serving candidate is incomplete.")
        if (
            run.state is not RunState.GRANTED
            or precompute.state is not NodeState.WAITING_INPUT
            or bake.state is not NodeState.PENDING
        ):
            raise GraphStoreUnavailableError("an unexecuted serving candidate already ran.")
        return [
            _node_settle(precompute, state, terminal_at),
            _node_settle(bake, state, terminal_at),
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND revision = :revision AND grant_id = :grant",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        succeeded=write_text(RunState.SUCCEEDED.value),
                        revision=write_int(run.revision),
                        grant=write_text(binding.grant_id),
                        one=write_int(1),
                        outcome=write_text(serving_outcome(outcome_binding or binding).value),
                        terminal_at=write_text(terminal_at),
                    ),
                ),
                "SET #state = :succeeded, revision = revision + :one, "
                "serving_outcome = :outcome, terminal_at = :terminal_at",
            ),
        ]

    def _direct_drain_members(
        self, request: ServingDirectDrainRequest, existing: ServingBinding | None
    ) -> list[Member]:
        """Persist direct truth while retaining immutable markers and cumulative frontier."""
        run = self.read_run(request.winner.run_id)
        if run is None:
            raise GraphStoreUnavailableError("the direct winner run disappeared.")
        due = due_shard(run.conflict_unit)
        remaining = tuple(
            run_id for run_id in request.head.elected_run_ids if run_id != request.winner.run_id
        )
        cohort_key = _key(
            due_shard(request.head.domain), f"COALESCE#{request.head.cohort_revision}"
        )
        cohort_member = (
            update(
                MemberSpec(
                    "",
                    cohort_key,
                    _SERVING_HEAD_CHANGED,
                    "winner_run_id = :winner",
                    _values(
                        winner=write_text(request.winner.run_id),
                        remaining=_list_text(remaining),
                        binding=write_text(request.direct.binding_id),
                    ),
                ),
                "SET run_ids = :remaining, direct_binding_id = :binding",
            )
            if remaining
            else delete(MemberSpec("", cohort_key, _SERVING_HEAD_CHANGED, "attribute_exists(pk)"))
        )
        return [
            self._binding_member(request.direct, existing, "direct_drain", request.winner.run_id),
            cohort_member,
            update(
                MemberSpec(
                    "",
                    _serving_head_key(request.head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :active "
                    "AND cohort_revision = :cohort AND elected_frontier_digest = :frontier "
                    "AND elected_candidate_digest = :candidates",
                    _values(
                        version=write_int(request.head.version),
                        active=write_text("ACTIVE"),
                        cohort=write_text(cast("str", request.head.cohort_revision)),
                        frontier=write_text(cast("str", request.head.elected_frontier_digest)),
                        candidates=write_text(cast("str", request.head.elected_candidate_digest)),
                        next=write_text("BOUND_DRAINING" if remaining else "IDLE"),
                        one=write_int(1),
                        binding=write_text(request.direct.binding_id),
                        grant=write_text(request.direct.grant_id),
                    ),
                ),
                "SET #state = :next, version = version + :one, "
                "direct_binding_id = :binding, direct_grant_id = :grant",
            ),
            delete(MemberSpec("", _key(due, f"ARM#{run.run_id}"), RUN_STATE_CHANGED)),
            delete(MemberSpec("", _key(due, f"COMPLETE#{run.run_id}"), RUN_STATE_CHANGED)),
        ]

    def drain_direct_binding(self, request: ServingDirectDrainRequest) -> ServingMutationResult:
        """Persist the direct drain, or prove its exact stored replay."""
        existing = self.read_serving_binding(request.direct.grant_id)
        existing_item = self._get(serving_binding_key(request.direct.grant_id))
        if (
            existing == request.direct
            and existing_item is not None
            and _text(existing_item, "terminal_operation") == "direct_drain"
        ):
            return ServingMutationResult.REPLAY
        self._commit(self._direct_drain_members(request, existing))
        return ServingMutationResult.COMMITTED

    def _validate_successor_snapshot(self, request: DrainFinalAliasAndActivateNextRequest) -> None:
        head_frontier = request.head.elected_frontier
        if head_frontier is None:
            raise GraphStoreUnavailableError("the active head has no immutable elected frontier.")
        if (
            self._candidate_digest(request.successor_candidates)
            != request.successor_candidate_digest
        ):
            raise GraphStoreUnavailableError("the successor candidate digest changed.")
        coordinates: dict[int, set[int]] = {shard: set() for shard in range(_SERVING_SHARDS)}
        for candidate in request.successor_candidates:
            marker = self._get(_serving_marker_key(candidate.run_id))
            if marker is None or _text(marker, "state") != "SETTLED_ELIGIBLE":
                raise GraphStoreUnavailableError("a successor publication marker is incomplete.")
            shard = _req_int(marker, "frontier_shard")
            coordinates[shard].add(_req_int(marker, "eligible_coordinate"))
        for shard in range(_SERVING_SHARDS):
            expected = set(
                range(
                    head_frontier.eligible_counts[shard] + 1,
                    request.live_frontier.eligible_counts[shard] + 1,
                )
            )
            if coordinates[shard] != expected:
                raise GraphStoreUnavailableError(
                    "serving candidate marker coordinates are incomplete."
                )

    def _alias_terminal_members(  # noqa: PLR0913, PLR0917 -- one alias, every identity explicit
        self,
        candidate: ServingCandidate,
        alias: ServingBinding,
        direct: ServingBinding,
        terminal_at: str,
    ) -> list[Member]:
        return self._unexecuted_candidate_terminal_members(
            candidate,
            alias,
            NodeState.COALESCED,
            terminal_at,
            outcome_binding=direct,
        )

    def drain_alias_binding(self, request: ServingAliasDrainRequest) -> ServingAliasDrainResult:
        """Drain one non-final alias while preserving its immutable publication marker."""
        if not request.remaining_run_ids:
            raise ValueError("the final alias requires drain_final_alias_and_activate_next.")
        existing = self.read_serving_binding(request.alias.grant_id)
        members = [
            check(
                MemberSpec(
                    "",
                    _serving_head_key(request.head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :draining AND direct_binding_id = :binding",
                    _values(
                        version=write_int(request.head.version),
                        draining=write_text("BOUND_DRAINING"),
                        binding=write_text(request.direct.binding_id),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    serving_binding_key(request.direct.grant_id),
                    RUN_STATE_CHANGED,
                    "binding_id = :binding AND binding_kind = :direct",
                    _values(
                        binding=write_text(request.direct.binding_id), direct=write_text("direct")
                    ),
                )
            ),
            self._binding_member(
                request.alias,
                existing,
                "alias_drain",
                request.candidate.run_id,
                request.identity,
            ),
            update(
                MemberSpec(
                    "",
                    _key(
                        due_shard(request.head.domain),
                        f"COALESCE#{request.head.cohort_revision}",
                    ),
                    _SERVING_HEAD_CHANGED,
                    "direct_binding_id = :binding",
                    _values(
                        binding=write_text(request.direct.binding_id),
                        remaining=_list_text(request.remaining_run_ids),
                    ),
                ),
                "SET run_ids = :remaining",
            ),
            *self._alias_terminal_members(
                request.candidate, request.alias, request.direct, request.terminal_at
            ),
        ]
        run = self.read_run(request.candidate.run_id)
        if run is None:
            raise GraphStoreUnavailableError("the alias run disappeared.")
        members.extend(
            [
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(run.conflict_unit), f"ARM#{run.run_id}"),
                        RUN_STATE_CHANGED,
                    )
                ),
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(run.conflict_unit), f"COMPLETE#{run.run_id}"),
                        RUN_STATE_CHANGED,
                    )
                ),
            ]
        )
        self._commit(members)
        return ServingAliasDrainResult(ServingMutationResult.COMMITTED)

    def _selected_winner_members(
        self, request: DrainFinalAliasAndActivateNextRequest
    ) -> list[Member]:
        winner = request.successor_winner
        if winner is None:
            return []
        run = self.read_run(winner.run_id)
        bake = self.read_node(winner.run_id, "bake")
        if run is None or bake is None:
            raise GraphStoreUnavailableError("the selected successor winner is incomplete.")
        due = due_shard(run.conflict_unit)
        # The marker's `target_digest` is the publication target digest (never the publish
        # node's effect digest); the immutable settled marker is conditioned on its strongly
        # read value and the winner's grant.
        marker = self._get(_serving_marker_key(run.run_id))
        if marker is None or _text(marker, "state") != "SETTLED_ELIGIBLE":
            raise GraphStoreUnavailableError("the selected successor marker is not eligible.")
        return [
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND grant_id = :grant AND grant_ordinal = :ordinal",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        grant=write_text(winner.grant_id),
                        ordinal=write_int(winner.grant_ordinal),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _key(due, f"COMPLETE#{run.run_id}"),
                    RUN_STATE_CHANGED,
                    "grant_id = :grant AND grant_ordinal = :ordinal AND #domain = :domain",
                    _values(
                        grant=write_text(winner.grant_id),
                        ordinal=write_int(winner.grant_ordinal),
                        domain=write_text(winner.domain),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _serving_marker_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :eligible AND grant_id = :grant AND target_digest = :digest",
                    _values(
                        eligible=write_text("SETTLED_ELIGIBLE"),
                        grant=write_text(winner.grant_id),
                        digest=write_text(_req_text(marker, "target_digest")),
                    ),
                )
            ),
            update(
                MemberSpec(
                    "",
                    _key(f"RUN#{run.run_id}", "NODE#bake"),
                    NODE_STATE_CHANGED,
                    "#state = :pending AND revision = :revision",
                    _values(
                        pending=write_text(NodeState.PENDING.value),
                        ready=write_text(NodeState.READY.value),
                        revision=write_int(bake.revision),
                        one=write_int(1),
                        cohort=write_text(cast("str", request.successor_cohort_revision)),
                        input=write_text(cast("str", request.successor_input_revision)),
                        plan=write_text(cast("str", request.successor_plan_digest)),
                        runs=_list_text(
                            tuple(item.run_id for item in request.successor_candidates)
                        ),
                        domain=write_text(winner.domain),
                        at=write_text(request.terminal_at),
                    ),
                ),
                "SET #state = :ready, revision = revision + :one, serving_domain = :domain, "
                "cohort_revision = :cohort, serving_input_revision = :input, "
                "serving_plan_digest = :plan, serving_run_ids = :runs, armed_at = :at",
            ),
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(f"TOKEN#{bake.node_id}", "TOKEN"),
                    "item_kind": write_text("token"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "run_id": write_text(run.run_id),
                    "node_id": write_text(bake.node_id),
                },
            ),
            put(
                MemberSpec("", {}, DOMAIN_LOCK_HELD, "attribute_not_exists(pk)"),
                {
                    **_key(f"LOCK#{bake.lock_key}", "LOCK"),
                    "item_kind": write_text("lock"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "holder_node_id": write_text(bake.node_id),
                    "run_id": write_text(run.run_id),
                },
            ),
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(due, f"OUTBOX#{bake.node_id}"),
                    "item_kind": write_text("outbox"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "run_id": write_text(run.run_id),
                    "node": write_text("bake"),
                    "generation": write_int(run.dispatch_generation),
                    "revision": write_int(1),
                    "state": write_text("OPEN"),
                    "due_at": write_text(request.terminal_at),
                    **root_clock_attribute(run.root_clock_started_at),
                },
            ),
            delete(MemberSpec("", _key(due, f"ARM#{run.run_id}"), RUN_STATE_CHANGED)),
        ]

    def _final_drain_members(self, request: DrainFinalAliasAndActivateNextRequest) -> list[Member]:
        run = self.read_run(request.final_candidate.run_id)
        existing = self.read_serving_binding(request.final_alias.grant_id)
        if run is None:
            raise GraphStoreUnavailableError("the final alias run disappeared.")
        due = due_shard(run.conflict_unit)
        return [
            check(
                MemberSpec(
                    "",
                    serving_binding_key(request.direct.grant_id),
                    RUN_STATE_CHANGED,
                    "binding_id = :binding AND binding_kind = :direct",
                    _values(
                        binding=write_text(request.direct.binding_id), direct=write_text("direct")
                    ),
                )
            ),
            self._binding_member(
                request.final_alias,
                existing,
                "alias_drain",
                request.final_candidate.run_id,
                request.identity,
            ),
            delete(
                MemberSpec(
                    "",
                    _key(
                        request.final_obligation.shard,
                        request.final_obligation.sk,
                    ),
                    _SERVING_HEAD_CHANGED,
                    "attribute_exists(pk)",
                )
            ),
            *self._alias_terminal_members(
                request.final_candidate,
                request.final_alias,
                request.direct,
                request.terminal_at,
            ),
            delete(MemberSpec("", _key(due, f"ARM#{run.run_id}"), RUN_STATE_CHANGED)),
            delete(MemberSpec("", _key(due, f"COMPLETE#{run.run_id}"), RUN_STATE_CHANGED)),
        ]

    def _drain_final_alias_and_activate_next_members(
        self, request: DrainFinalAliasAndActivateNextRequest
    ) -> list[Member]:
        self._validate_successor_snapshot(request)
        frontier = self._frontier_checks(request.head.domain, request.live_frontier)
        final = self._final_drain_members(request)
        selected = self._selected_winner_members(request)
        elected = request.successor_winner is not None
        next_state = "ACTIVE" if elected else "IDLE"
        head_values: dict[str, AttributeValueTypeDef] = {
            "version": write_int(request.head.version),
            "active": write_text("BOUND_DRAINING"),
            "binding": write_text(request.direct.binding_id),
            "next": write_text(next_state),
            "one": write_int(1),
        }
        head_expression = "SET #state = :next, version = version + :one"
        shared: list[Member] = []
        if elected:
            if request.successor_winner is None:
                raise GraphStoreUnavailableError("an elected transition has no winner.")
            snapshot = _elected_snapshot_attributes(
                request.live_frontier,
                request.successor_candidate_digest,
                tuple(item.run_id for item in request.successor_candidates),
            )
            snapshot.update(
                cohort_revision=write_text(cast("str", request.successor_cohort_revision)),
                serving_input_revision=write_text(cast("str", request.successor_input_revision)),
                winner_run_id=write_text(request.successor_winner.run_id),
            )
            for name, value in snapshot.items():
                head_values[name] = value
                head_expression += f", {name} = :{name}"
        shared.append(
            update(
                MemberSpec(
                    "",
                    _serving_head_key(request.head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :active AND direct_binding_id = :binding",
                    {f":{name}": value for name, value in head_values.items()},
                ),
                head_expression,
            )
        )
        if elected:
            shared.append(
                put(
                    MemberSpec("", {}, _SERVING_HEAD_CHANGED, "attribute_not_exists(pk)"),
                    {
                        **_key(
                            due_shard(request.head.domain),
                            f"COALESCE#{request.successor_cohort_revision}",
                        ),
                        "item_kind": write_text("coalesce"),
                        "domain": write_text(request.head.domain),
                        "winner_run_id": write_text(
                            cast("ServingCandidate", request.successor_winner).run_id
                        ),
                        "run_ids": _list_text(
                            tuple(item.run_id for item in request.successor_candidates)
                        ),
                        "plan_digest": write_text(cast("str", request.successor_plan_digest)),
                        "due_at": write_text(request.terminal_at),
                        **root_clock_attribute(cohort_root_clock(request.successor_candidates)),
                    },
                )
            )
        transition: Item = {
            **_key(
                f"SERVING_TRANSITION#{request.head.domain}",
                f"COHORT#{request.head.cohort_revision}",
            ),
            "item_kind": write_text("serving_transition"),
            "domain": write_text(request.head.domain),
            "drained_cohort_revision": write_text(cast("str", request.head.cohort_revision)),
            "aws_request_id": write_text(request.aws_request_id),
            "drained_at": write_text(request.terminal_at),
            "outcome": write_text("ELECTED" if elected else "NO_ELIGIBLE"),
        }
        if elected:
            transition.update(
                elected_cohort_revision=write_text(cast("str", request.successor_cohort_revision)),
                elected_at=write_text(request.terminal_at),
            )
        shared.append(
            put(MemberSpec("", {}, _SERVING_HEAD_CHANGED, "attribute_not_exists(pk)"), transition)
        )
        members = [*frontier, *final, *selected, *shared]
        expected = 51 if elected else 42
        if len(members) != expected:
            raise GraphStoreUnavailableError(
                f"fixed-member final drain built {len(members)} actions, expected {expected}."
            )
        return members

    def read_serving_promoter_transition(
        self, domain: str, drained_cohort_revision: str
    ) -> ServingPromoterTransition | None:
        """Strongly read the drained cohort's immutable transition record."""
        item = self._get(_key(f"SERVING_TRANSITION#{domain}", f"COHORT#{drained_cohort_revision}"))
        if item is None:
            return None
        return ServingPromoterTransition(
            domain=domain,
            drained_cohort_revision=drained_cohort_revision,
            aws_request_id=_req_text(item, "aws_request_id"),
            drained_at=_req_text(item, "drained_at"),
            elected_cohort_revision=_text(item, "elected_cohort_revision"),
            elected_at=_text(item, "elected_at"),
        )

    def _final_transition_replay_change(
        self, request: DrainFinalAliasAndActivateNextRequest, head: ServingDomainHead | None
    ) -> str | None:
        """Name the first committed transition or resulting-head fact the request does not equal."""
        item = self._get(
            _key(
                f"SERVING_TRANSITION#{request.head.domain}",
                f"COHORT#{request.head.cohort_revision}",
            )
        )
        if item is None:
            return "transition"
        winner = request.successor_winner
        elected_at = request.terminal_at if winner is not None else None
        facts: list[tuple[object, object, str]] = [
            (
                _text(item, "outcome"),
                "ELECTED" if winner is not None else "NO_ELIGIBLE",
                "outcome",
            ),
            (
                _text(item, "elected_cohort_revision"),
                request.successor_cohort_revision,
                "successor cohort revision",
            ),
            (_text(item, "drained_at"), request.terminal_at, "terminal time"),
            (_text(item, "elected_at"), elected_at, "terminal time"),
        ]
        if head is None:
            facts.append((None, request.head.domain, "resulting head"))
        elif winner is None:
            facts.append(
                (
                    (head.state, head.version, head.cohort_revision),
                    ("IDLE", request.head.version + 1, request.head.cohort_revision),
                    "resulting head",
                )
            )
        else:
            facts.extend(self._elected_replay_facts(request, winner, head))
        return next((fact for committed, requested, fact in facts if committed != requested), None)

    def _elected_replay_facts(
        self,
        request: DrainFinalAliasAndActivateNextRequest,
        winner: ServingCandidate,
        head: ServingDomainHead,
    ) -> list[tuple[object, object, str]]:
        """Pair the activated head snapshot and stamped winner bake with the request's facts."""
        run_ids = tuple(item.run_id for item in request.successor_candidates)
        bake = self.read_node(winner.run_id, "bake")
        stamped = (
            None
            if bake is None
            else (
                bake.cohort_revision,
                bake.serving_input_revision,
                bake.serving_plan_digest,
                bake.serving_run_ids,
            )
        )
        return [
            (
                (
                    head.state,
                    head.version,
                    head.cohort_revision,
                    head.serving_input_revision,
                    head.winner_run_id,
                    head.elected_frontier,
                    head.elected_frontier_digest,
                    head.elected_candidate_digest,
                    head.elected_run_ids,
                ),
                (
                    "ACTIVE",
                    request.head.version + 1,
                    request.successor_cohort_revision,
                    request.successor_input_revision,
                    winner.run_id,
                    request.live_frontier,
                    request.live_frontier.digest,
                    request.successor_candidate_digest,
                    run_ids,
                ),
                "resulting head",
            ),
            (
                stamped,
                (
                    request.successor_cohort_revision,
                    request.successor_input_revision,
                    request.successor_plan_digest,
                    run_ids,
                ),
                "successor winner",
            ),
        ]

    def drain_final_alias_and_activate_next(
        self, request: DrainFinalAliasAndActivateNextRequest
    ) -> DrainFinalAliasAndActivateNextResult:
        """Commit the fixed-member final drain/activation, or resolve its equal replay."""
        prior = self.read_serving_promoter_transition(
            request.head.domain, cast("str", request.head.cohort_revision)
        )
        if prior is not None:
            if prior.aws_request_id != request.aws_request_id:
                raise GraphStoreUnavailableError("a promoter transition replay changed request id.")
            domain = self.read_serving_domain(request.head.domain)
            changed = self._final_transition_replay_change(request, domain.head)
            if changed is not None:
                raise GraphStoreUnavailableError(f"a promoter transition replay changed {changed}.")
            outcome = (
                ServingElectionOutcome.ELECTED
                if prior.elected_cohort_revision is not None
                else ServingElectionOutcome.NO_ELIGIBLE
            )
            return DrainFinalAliasAndActivateNextResult(
                ServingMutationResult.REPLAY, outcome, prior, domain.head
            )
        members = self._drain_final_alias_and_activate_next_members(request)
        self._commit(members)
        transition = cast(
            "ServingPromoterTransition",
            self.read_serving_promoter_transition(
                request.head.domain, cast("str", request.head.cohort_revision)
            ),
        )
        domain = self.read_serving_domain(request.head.domain)
        outcome = (
            ServingElectionOutcome.ELECTED
            if request.successor_winner is not None
            else ServingElectionOutcome.NO_ELIGIBLE
        )
        return DrainFinalAliasAndActivateNextResult(
            ServingMutationResult.COMMITTED, outcome, transition, domain.head
        )

    def reconcile_serving_outcome(
        self, request: ServingOutcomeReconciliationRequest
    ) -> ServingOutcomeReconciliationResult:
        """Return aggregate authority truth only after every alias proves the direct binding."""
        return ServingOutcomeReconciliationResult(
            outcome=aggregate_serving_outcome(request.direct, request.aliases),
            direct_binding_id=request.direct.binding_id,
            alias_count=len(request.aliases),
            authority_digest=request.authority_digest,
        )

    def record_post_grant_failure(self, failure: PostGrantFailure) -> None:
        """Block the domain, fail the candidate and the run (BF-4); no undefined state remains."""
        run, grant, at = failure.run, failure.grant, failure.recorded_at
        members: list[Member] = [
            put(
                MemberSpec("", {}, CONFLICT_DOMAIN_BLOCKED, "attribute_not_exists(pk)"),
                {
                    **_key(f"BLOCK#{grant.domain}", "BLOCK"),
                    "item_kind": write_text("block"),
                    "block_reason": write_text(failure.reason),
                    "run_id": write_text(run.run_id),
                    "grant_id": write_text(grant.grant_id),
                    "conflict_unit": write_text(run.conflict_unit),
                    "blocked_at": write_text(at),
                },
            ),
            update(
                MemberSpec(
                    "",
                    _key(f"LINEAGE#{run.conflict_unit}", "HEAD"),
                    RUN_STATE_CHANGED,
                    "tail_grant_id = :grant",
                    _values(grant=write_text(grant.grant_id)),
                ),
                "SET failed_candidate = :grant",
            ),
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :open AND grant_id = :grant",
                    _values(
                        open=write_text(RunState.OPEN.value),
                        failed=write_text(RunState.FAILED.value),
                        grant=write_text(grant.grant_id),
                        one=write_int(1),
                        yes=write_bool(True),
                        reason=write_text(failure.reason),
                    ),
                ),
                "SET #state = :failed, lineage_failed = :yes, failure_reason = :reason, "
                "revision = revision + :one",
            ),
            delete(
                MemberSpec(
                    "",
                    _key(due_shard(run.conflict_unit), f"APPLY#{run.run_id}"),
                    RUN_STATE_CHANGED,
                )
            ),
        ]
        if failure.slot_owned:
            members.extend((_slot_release(run), _wake(run, at)))
        if failure.order is not None:
            # The block, not the ordinal, holds the domain until owner review.
            members.append(_order_release(failure.order, run.run_id, None))
        self._commit(members)

    def complete_granted_run(  # noqa: PLR0913 -- the design-exact keyword-only terminal_at
        self,
        run: RunRecord,
        nodes: tuple[NodeRecord, ...],
        drain: ServingDirectDrainRequest,
        *,
        terminal_at: str,
    ) -> ServingMutationResult:
        """GRANTED -> SUCCEEDED with caller-owned terminal time and direct drain.

        It also deletes each FAILED roster task's `BAKE_RECOVERY#`: the winner's completion is
        the settlement that obligation names as its deleter (ADR-140, B4).
        """
        binding = drain.direct
        if self.read_serving_binding(binding.grant_id) != binding:
            raise GraphGuardError(RUN_STATE_CHANGED)
        completed = self._get(_run_key(run.run_id))
        if (
            completed is not None
            and _text(completed, "state") == RunState.SUCCEEDED.value
            and _text(completed, "terminal_at") == terminal_at
        ):
            return ServingMutationResult.REPLAY
        members: list[Member] = [
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND revision = :rev",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        succeeded=write_text(RunState.SUCCEEDED.value),
                        rev=write_int(run.revision),
                        one=write_int(1),
                        outcome=write_text(serving_outcome(binding).value),
                        terminal=write_text(terminal_at),
                    ),
                ),
                "SET #state = :succeeded, revision = revision + :one, "
                "serving_outcome = :outcome, terminal_at = :terminal",
            ),
            *self._direct_drain_members(drain, binding),
            *(
                check(
                    MemberSpec(
                        "",
                        _key(f"RUN#{node.run_id}", f"NODE#{node.name}"),
                        NODE_STATE_CHANGED,
                        "#state = :s AND revision = :rev",
                        _values(s=write_text(node.state.value), rev=write_int(node.revision)),
                    )
                )
                for node in nodes
            ),
            # ADR-140 (B4): the completion settles the winner, so it deletes the recovery
            # obligation of each FAILED roster task (at most one per roster row, unconditional).
            *(
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(run.conflict_unit), f"BAKE_RECOVERY#{node.node_id}"),
                        NODE_STATE_CHANGED,
                    )
                )
                for node in nodes
                if template_node(node.name) == BAKE_TASK_NODE and node.state is NodeState.FAILED
            ),
        ]
        self._commit(members)
        return ServingMutationResult.COMMITTED

    # --- the cohort and the serving binding -----------------------------------------------------

    def record_contribution(self, contribution: CohortContribution) -> None:
        """Write one immutable contribution; an equal retry is a no-op, a different one fails."""
        existing = [
            item
            for item in self.read_contributions(contribution.grant_id)
            if item.member is contribution.member
        ]
        if existing:
            if existing[0] != contribution:
                raise GraphStoreUnavailableError("a sibling contributed two different terminals.")
            return
        self._commit(
            [
                put(
                    MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                    _contribution_item(contribution),
                )
            ]
        )

    def read_contributions(self, grant_id: str) -> tuple[CohortContribution, ...]:
        """Read every contribution of one grant's cohort."""
        return tuple(
            CohortContribution(
                grant_id=grant_id,
                member=CohortMember(_req_text(item, "sk").removeprefix("CONTRIBUTION#")),
                outcome=CohortOutcome(_req_text(item, "outcome")),
                artifact_revision=_text(item, "artifact_revision"),
                settled_at=datetime.fromisoformat(_req_text(item, "settled_at")),
                root_flow_id=_req_text(item, "root_flow_id"),
                unavailable=_read_unavailable(item),
                input_effect_roster_digest=_text(item, "input_effect_roster_digest"),
                request_roster=_read_pair_list(item, "request_roster"),
            )
            for item in self._query(f"COHORT#{grant_id}", "CONTRIBUTION#")
        )

    def bind_serving(self, binding: ServingBinding) -> None:
        """Write the immutable serving binding of one settled cohort."""
        self._commit(
            [
                put(
                    MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                    _serving_binding_item(binding),
                )
            ]
        )

    def read_serving_binding(self, grant_id: str) -> ServingBinding | None:
        """Read one grant's serving binding."""
        item = self._get(serving_binding_key(grant_id))
        return None if item is None else serving_binding_from_item(grant_id, item)

    def read_node(self, run_id: str, name: str) -> NodeRecord | None:
        """Read one node by its template name."""
        item = self._get(_key(f"RUN#{run_id}", f"NODE#{name}"))
        return None if item is None else read_node_item(item)

    # --- ADR-140: the unarmed settlement --------------------------------------------------------

    def _unarmed_image(self, settlement: UnarmedPrecomputeSettlement) -> str:
        """Classify the stored image of the unarmed settlement: post, pre or mixed."""
        grant = settlement.binding.grant_id
        stored = {item.member: item for item in self.read_contributions(grant)}
        node = self.read_node(settlement.run.run_id, settlement.node.name)
        binding = self.read_serving_binding(grant)
        if (
            stored.get(CohortMember.PRECOMPUTE) == settlement.precompute
            and node is not None
            and node.state is NodeState.SUCCEEDED
            and node.effect_digest == settlement.effect_digest
            and binding == settlement.binding
        ):
            return "post"
        untouched = (
            CohortMember.PRECOMPUTE not in stored
            and node is not None
            and node.state is NodeState.WAITING_INPUT
            and node.revision == settlement.node.revision
            and binding is None
        )
        return "pre" if untouched else "mixed"

    def _unarmed_members(self, settlement: UnarmedPrecomputeSettlement) -> list[Member]:
        run, head, bake = settlement.run, settlement.head, settlement.bake
        return [
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND revision = :rev AND grant_id = :grant",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        rev=write_int(run.revision),
                        grant=write_text(settlement.binding.grant_id),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _serving_head_key(head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :active AND winner_run_id = :run "
                    "AND cohort_revision = :cohort",
                    _values(
                        version=write_int(head.version),
                        active=write_text("ACTIVE"),
                        run=write_text(run.run_id),
                        cohort=write_text(cast("str", head.cohort_revision)),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _key(f"COHORT#{bake.grant_id}", "CONTRIBUTION#bake"),
                    NODE_STATE_CHANGED,
                    "outcome = :outcome AND settled_at = :settled",
                    _values(
                        outcome=write_text(bake.outcome.value),
                        settled=write_text(bake.settled_at.isoformat()),
                    ),
                )
            ),
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                _contribution_item(settlement.precompute),
            ),
            node_transition_member(
                settlement.node,
                NodeState.SUCCEEDED,
                {
                    "settled_at": write_text(settlement.recorded_at),
                    "effect_digest": write_text(settlement.effect_digest),
                    "effect_record": write_text(settlement.effect_record),
                },
            ),
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                _serving_binding_item(settlement.binding),
            ),
        ]

    def settle_unarmed_precompute(
        self, settlement: UnarmedPrecomputeSettlement
    ) -> ServingMutationResult:
        """ADR-140: settle precompute `NO_ELIGIBLE` and bind the cohort in one transaction.

        Six members: checks on `RUN#`, the ACTIVE head and the stored bake member; the new
        precompute member; the `WAITING_INPUT` node's move to `SUCCEEDED`; the direct binding.
        Response loss follows `arm_precompute_from_bake_effects`: an exact post-image is
        `REPLAY`, a pre-image retries once, anything else is `GRAPH_PRECOMPUTE/6`.
        """
        if self._unarmed_image(settlement) == "post":
            return ServingMutationResult.REPLAY
        members = self._unarmed_members(settlement)
        for _ in range(2):
            try:
                self._commit(members)
            except GraphStoreUnavailableError:
                image = self._unarmed_image(settlement)
                if image == "post":
                    return ServingMutationResult.REPLAY
                if image == "mixed":
                    raise InvariantError(
                        UNARMED_PRECOMPUTE_IMAGE_MIXED,
                        "the unarmed settlement's stored image is neither pre nor post.",
                        context={"run_id": settlement.run.run_id},
                    ) from None
                continue
            return ServingMutationResult.COMMITTED
        raise GraphStoreUnavailableError("the unarmed settlement transaction stayed uncertain.")

    # --- GRAPH bake V3: the source pin (plan 4.3, S5, DB21) -----------------------------------

    @staticmethod
    def _guard_bake_pin(request: BakeSourcePinRequest, members: list[Member]) -> None:
        """Refuse an unbounded or over-envelope pin before any mutation (A5.1 I9, M4).

        Every `current_for_scope` read in a row blob must carry its horizon token, so pin bytes
        can't depend on retained history; then every resulting item must fit 400,000 and the
        transaction 4,000,000 conservative bytes, measured as every graph transaction is.
        """
        for task, blob in zip(request.manifest.tasks, request.reads, strict=True):
            try:
                decoded = cast("list[list[Any]]", json.loads(gzip.decompress(blob)))
                keys = [(str(read[0]), list(read[1])) for read in decoded]
            except OSError, ValueError, TypeError, IndexError:
                raise DurableBakeSourceManifestInvalidError("stored reads do not decode") from None
            for kind, key in keys:
                if kind == _SCOPE_READ and (
                    len(key) != 2 or not _HORIZON_TOKEN.fullmatch(str(key[1]))  # noqa: PLR2004
                ):
                    raise InvariantError(
                        _BAKE_FAN_OUT_FAILED,
                        "a bake source pin holds an unbounded scope read",
                        context={
                            "signal": "bake_source_pin_unbounded_scope_read",
                            "page": task.page,
                            "variant": task.variant,
                            "scope": str(key[0]) if key else "",
                        },
                    )
        sized = [
            (str(body["Item"]["sk"]["S"]), item_bytes(body["Item"]))
            for member in members
            for kind, body in cast("dict[str, dict[str, Any]]", member.item).items()
            if kind == "Put"
        ]
        largest_sk, largest = max(sized, key=lambda item: item[1])
        aggregate = transaction_bytes(members, {})
        if largest > MAX_CONSERVATIVE_SERVICE_ITEM_BYTES or aggregate > MAX_TRANSACTION_BYTES:
            raise InvariantError(
                _BAKE_FAN_OUT_FAILED,
                "the bake source pin exceeds the transaction byte envelope.",
                context={
                    "signal": "bake_source_pin_over_byte_envelope",
                    "largest_item_bytes": largest,
                    "largest_item_sk": largest_sk,
                    "aggregate_bytes": aggregate,
                },
            )

    def _bake_pin_items(self, request: BakeSourcePinRequest) -> list[Item]:
        manifest = request.manifest
        run_pk = f"RUN#{request.run.run_id}"
        items: list[Item] = [
            {
                **_key(run_pk, f"BAKE_SOURCE#{task.key}"),
                "item_kind": write_text("bake_source"),
                "page": write_text(task.page),
                "variant": write_text(task.variant),
                "reads": {"B": blob},
                "reads_digest": write_text(task.reads_digest),
                "request_digests": {"L": [write_text(item) for item in task.request_digests]},
                "plan_count": write_int(task.plan_count),
                "source_digest": write_text(task.digest),
                # ADR-142: optional, written only when set, so a stored pin re-hashes unchanged.
                **(
                    {}
                    if task.pin_refusal is None
                    else {
                        "pin_refusal": write_text(task.pin_refusal.reason),
                        "pin_refusal_attempts": write_int(task.pin_refusal.attempts),
                    }
                ),
            }
            for task, blob in zip(manifest.tasks, request.reads, strict=True)
        ]
        items.append(
            {
                **_key(run_pk, "BAKE_PIN"),
                "item_kind": write_text("bake_pin"),
                "grant_id": write_text(manifest.grant_id),
                "roster_digest": write_text(manifest.roster_digest),
                "manifest_digest": write_text(manifest.digest),
                "task_keys": {"L": [write_text(task.key) for task in manifest.tasks]},
                "plan_counts": {"L": [write_int(count) for count in manifest.plan_counts]},
                "plan_counts_digest": write_text(manifest.plan_counts_digest),
                "pinned_at": write_text(request.recorded_at),
            }
        )
        return items

    def _bake_pin_members(self, request: BakeSourcePinRequest) -> list[Member]:
        run, head = request.run, request.head
        members: list[Member] = [
            check(
                MemberSpec(
                    "",
                    _key(f"EXEC_CLAIM#{request.claim.node_id}", "CLAIM"),
                    CLAIM_LEASE_STALE,
                    "lease_version = :version",
                    _values(version=write_int(request.claim.lease_version)),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND grant_id = :grant",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        grant=write_text(request.manifest.grant_id),
                    ),
                )
            ),
            check(
                MemberSpec(
                    "",
                    _serving_head_key(head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :active AND winner_run_id = :run "
                    "AND serving_input_revision = :input",
                    _values(
                        version=write_int(head.version),
                        active=write_text("ACTIVE"),
                        run=write_text(run.run_id),
                        input=write_text(head.serving_input_revision or ""),
                    ),
                )
            ),
        ]
        members.extend(
            put(MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"), item)
            for item in self._bake_pin_items(request)
        )
        return members

    def read_durable_bake_sources(self, run_id: str) -> PinnedBakeSources | None:
        """Strongly read a winner's pin; a pin that does not re-hash is the fatal refusal."""
        items = self._query(f"RUN#{run_id}", "BAKE_")
        pin = next((item for item in items if _text(item, "sk") == "BAKE_PIN"), None)
        sources = {
            _req_text(item, "sk").removeprefix("BAKE_SOURCE#"): item
            for item in items
            if _req_text(item, "sk").startswith("BAKE_SOURCE#")
        }
        if pin is None:
            if sources:
                raise DurableBakeSourceManifestInvalidError("source items without their pin")
            return None
        keys = _string_list(pin, "task_keys")
        if set(keys) != set(sources):
            raise DurableBakeSourceManifestInvalidError("the pin names other source items")
        tasks = tuple(
            BakeTaskSource(
                _req_text(sources[key], "page"),
                _req_text(sources[key], "variant"),
                _req_text(sources[key], "reads_digest"),
                _string_list(sources[key], "request_digests"),
                _req_int(sources[key], "plan_count"),
                _pin_refusal(sources[key]),
            )
            for key in keys
        )
        manifest = DurableBakeSourceManifest(
            run_id, _req_text(pin, "grant_id"), _req_text(pin, "roster_digest"), tasks
        )
        if manifest.digest != _req_text(pin, "manifest_digest"):
            raise DurableBakeSourceManifestInvalidError("the stored pin does not re-hash")
        blobs = tuple(bytes(cast("Any", sources[key]["reads"])["B"]) for key in keys)
        return PinnedBakeSources(manifest, blobs)

    def _bake_pin_image(self, request: BakeSourcePinRequest) -> str:
        """Classify the stored image after an uncertain pin: `post`, `pre` or `mixed`."""
        expected = self._bake_pin_items(request)
        found = [self._get(_key(_req_text(item, "pk"), _req_text(item, "sk"))) for item in expected]
        if all(item is None for item in found):
            return "pre"
        if all(item == wanted for item, wanted in zip(found, expected, strict=True)):
            return "post"
        return "mixed"

    def pin_durable_bake_sources(self, request: BakeSourcePinRequest) -> BakeSourcePinOutcome:
        """Pin the winner's manifest, N plan counts and their digest in one transaction (S5).

        An existing pin is adopted only when it is exactly this manifest; any other is fatal.
        Response loss is classified from the stored image: the exact post-image is adopted, a
        complete pre-image retries the same bytes once, and a mixed image is fatal. A refused
        condition (claim lease, grant or winner head) raises its guard: zero effect.
        """
        stored = self.read_durable_bake_sources(request.run.run_id)
        if stored is not None:
            if stored.manifest != request.manifest or stored.reads != request.reads:
                raise DurableBakeSourceManifestInvalidError("another manifest is pinned")
            return BakeSourcePinOutcome.ADOPTED
        members = self._bake_pin_members(request)
        self._guard_bake_pin(request, members)
        for _ in range(2):
            try:
                self._commit(members)
            except GraphStoreUnavailableError:
                image = self._bake_pin_image(request)
                if image == "post":
                    return BakeSourcePinOutcome.ADOPTED
                if image == "mixed":
                    raise DurableBakeSourceManifestInvalidError("a mixed pin image") from None
                continue
            return BakeSourcePinOutcome.PINNED
        raise GraphStoreUnavailableError("the bake source pin stayed uncertain.")

    # --- GRAPH bake V3: fan-out, task failure and the final transaction (plan 4.6, A3) --------

    def _winner_check(self, run: RunRecord, head: ServingDomainHead) -> Member:
        return check(
            MemberSpec(
                "",
                _serving_head_key(head.domain),
                _SERVING_HEAD_CHANGED,
                "version = :version AND #state = :active AND winner_run_id = :run",
                _values(
                    version=write_int(head.version),
                    active=write_text("ACTIVE"),
                    run=write_text(run.run_id),
                ),
            )
        )

    def _grant_check(self, run: RunRecord) -> Member:
        return check(
            MemberSpec(
                "",
                _run_key(run.run_id),
                RUN_STATE_CHANGED,
                "#state = :granted AND grant_id = :grant",
                _values(
                    granted=write_text(RunState.GRANTED.value),
                    grant=write_text(run.grant_id or ""),
                ),
            )
        )

    def fan_out_bake_tasks(self, request: BakeFanOutRequest) -> None:
        """Arm every roster task on its lane and put the finalizer, all or none (plan 4.6).

        `4N + 6` members: the claim, grant, winner-head and stored-pin conditions; per task its
        READY node, token, lock and lane outbox; the PENDING finalizer; and `ARM#` (serving),
        re-put so the arm pass arms the finalizer once every task is terminal. A lost response
        whose post-image is complete is success; nothing here retries a partial image.
        """
        run = request.run
        members: list[Member] = [
            check(
                MemberSpec(
                    "",
                    _key(f"EXEC_CLAIM#{request.claim.node_id}", "CLAIM"),
                    CLAIM_LEASE_STALE,
                    "lease_version = :version",
                    _values(version=write_int(request.claim.lease_version)),
                )
            ),
            self._grant_check(run),
            self._winner_check(run, request.head),
            check(
                MemberSpec(
                    "",
                    _key(f"RUN#{run.run_id}", "BAKE_PIN"),
                    NODE_STATE_CHANGED,
                    "manifest_digest = :digest",
                    _values(digest=write_text(request.manifest_digest)),
                )
            ),
        ]
        for spec in request.tasks:
            members.extend(self.armed_node_members(run, spec, request.recorded_at))
        members.append(self.pending_node_member(run, request.finalize, request.recorded_at))
        arm = due_obligation_item(
            run, "arm", request.recorded_at, scope=write_text(ArmScope.SERVING.value)
        )
        for name, value in request.arm_attributes.items():
            arm[name] = write_int(value) if isinstance(value, int) else write_text(value)
        members.append(put(MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"), arm))
        try:
            self._commit(members)
        except GraphStoreUnavailableError:
            names = {node.name for node in self.read_nodes(run.run_id)}
            if {spec.name for spec in (*request.tasks, request.finalize)} <= names:
                return
            raise

    def settle_bake_task_failure(self, failure: BakeTaskFailure) -> None:
        """Settle a task FAILED and write its `BAKE_RECOVERY#` DUE obligation together (F9).

        The obligation carries the closed reason and the run's root clock, so the stuck-flow
        sweep counts it; only the transaction that settles or obsoletes the winner deletes it.
        """
        run, node = failure.run, failure.node
        if failure.reason not in BAKE_V3["recovery_reasons"]:
            raise ValueError("a bake task failure names a closed recovery reason.")
        members = self._settle_members(
            NodeSettlement(
                run,
                node,
                NodeState.FAILED,
                failure.recorded_at,
                claim=failure.claim,
                failure_reason=failure.reason,
                failure_code=failure.failure_code,
            )
        )
        members.append(
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(due_shard(run.conflict_unit), f"BAKE_RECOVERY#{node.node_id}"),
                    "item_kind": write_text("bake_recovery"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "run_id": write_text(run.run_id),
                    "task_id": write_text(node.node_id),
                    "task": write_text(node.name),
                    "reason": write_text(failure.reason),
                    "due_at": write_text(failure.recorded_at),
                    **root_clock_attribute(run.root_clock_started_at),
                },
            )
        )
        self._commit(members)

    def read_precompute_input(self, run_id: str) -> PrecomputeInputV1 | None:
        """Read and attest the exact DB25 input stored on the precompute node."""
        item = self._get(_key(f"RUN#{run_id}", "NODE#precompute"))
        if item is None:
            return None
        text = _text(item, "precompute_input")
        digest = _text(item, "input_effect_roster_digest")
        if text is None and digest is None:
            return None
        if text is None or digest is None:
            raise GraphStoreUnavailableError("a precompute readiness input is partial.")
        try:
            parsed = PrecomputeInputV1.from_json(text)
        except TypeError, ValueError:
            raise GraphStoreUnavailableError("a precompute readiness input is malformed.") from None
        if parsed.digest != digest:
            raise GraphStoreUnavailableError("a precompute readiness input digest does not match.")
        return parsed

    def _precompute_readiness_image(self, request: PrecomputeReadinessRequest) -> str:
        node_item = self._get(_key(f"RUN#{request.run.run_id}", "NODE#precompute"))
        complete = self._get(
            _key(due_shard(request.run.conflict_unit), f"COMPLETE#{request.run.run_id}")
        )
        if node_item is None or complete is None:
            return "mixed"
        digest = request.input.digest
        post = (
            _text(node_item, "state") == NodeState.READY.value
            and _text(node_item, "input_effect_roster_digest") == digest
            and _text(node_item, "precompute_input") == request.input.to_json()
            and _text(complete, "input_effect_roster_digest") == digest
            and _text(complete, "bake_readiness_at") == request.recorded_at
            and self._get(_key(f"TOKEN#{request.node.node_id}", "TOKEN")) is not None
            and self._get(
                _key(due_shard(request.run.conflict_unit), f"OUTBOX#{request.node.node_id}")
            )
            is not None
        )
        if post:
            return "post"
        pre = (
            _text(node_item, "state") == str(BAKE_V3["waiting_input_state"])
            and _text(node_item, "input_effect_roster_digest") is None
            and _text(node_item, "precompute_input") is None
            and _text(complete, "input_effect_roster_digest") is None
            and _text(complete, "bake_readiness_at") is None
            and self._get(_key(f"TOKEN#{request.node.node_id}", "TOKEN")) is None
            and self._get(
                _key(due_shard(request.run.conflict_unit), f"OUTBOX#{request.node.node_id}")
            )
            is None
        )
        return "pre" if pre else "mixed"

    def arm_precompute_from_bake_effects(
        self, request: PrecomputeReadinessRequest
    ) -> PrecomputeReadinessOutcome:
        """Arm the existing route only after every exact roster effect is durable (DB25)."""
        if self._precompute_readiness_image(request) == "post":
            return PrecomputeReadinessOutcome.ADOPTED
        run, digest = request.run, request.input.digest
        members: list[Member] = [
            check(
                MemberSpec(
                    "",
                    _key(f"EXEC_CLAIM#{request.claim.node_id}", "CLAIM"),
                    CLAIM_LEASE_STALE,
                    "lease_version = :version",
                    _values(version=write_int(request.claim.lease_version)),
                )
            ),
            self._grant_check(run),
            self._winner_check(run, request.head),
        ]
        members.extend(
            check(
                MemberSpec(
                    "",
                    _key(f"RUN#{run.run_id}", f"NODE#{task.name}"),
                    NODE_STATE_CHANGED,
                    "#state = :ok AND effect_digest = :digest AND revision = :rev",
                    _values(
                        ok=write_text(NodeState.SUCCEEDED.value),
                        digest=write_text(effect.effect_digest),
                        rev=write_int(task.revision),
                    ),
                )
            )
            for task, effect in zip(request.tasks, request.effects, strict=True)
        )
        members.extend(
            arming_members(
                run,
                request.node,
                request.recorded_at,
                attributes={
                    "precompute_input": write_text(request.input.to_json()),
                    "input_effect_roster_digest": write_text(digest),
                },
            )
        )
        members.append(
            update(
                MemberSpec(
                    "",
                    _key(due_shard(run.conflict_unit), f"COMPLETE#{run.run_id}"),
                    RUN_STATE_CHANGED,
                    "attribute_exists(pk) AND attribute_not_exists(bake_readiness_at)",
                    _values(at=write_text(request.recorded_at), digest=write_text(digest)),
                ),
                "SET bake_readiness_at = :at, input_effect_roster_digest = :digest",
            )
        )
        if len(members) > int(BOUNDS["max_transaction_items"]):
            raise ValueError("precompute readiness exceeds the transaction member bound.")
        for _ in range(2):
            try:
                self._commit(members)
            except GraphStoreUnavailableError:
                image = self._precompute_readiness_image(request)
                if image == "post":
                    return PrecomputeReadinessOutcome.ADOPTED
                if image == "mixed":
                    raise InvariantError(
                        ErrorCode("GRAPH_PRECOMPUTE", 4),
                        "the readiness transaction's stored image is neither pre nor post.",
                    ) from None
                continue
            return PrecomputeReadinessOutcome.ARMED
        raise GraphStoreUnavailableError("the precompute readiness transaction stayed uncertain.")

    def _final_members(self, request: BakeFinalizeRequest) -> list[Member]:
        run, head, binding = request.run, request.head, request.binding
        if (
            head.cohort_revision is None
            or head.elected_frontier_digest is None
            or head.elected_candidate_digest is None
        ):
            raise GraphStoreUnavailableError(
                "a bake finalization requires immutable cohort authority."
            )
        remaining = tuple(run_id for run_id in head.elected_run_ids if run_id != run.run_id)
        cohort_key = _key(due_shard(head.domain), f"COALESCE#{head.cohort_revision}")
        due = due_shard(run.conflict_unit)
        members: list[Member] = [
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND revision = :revision AND grant_id = :grant",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        succeeded=write_text(RunState.SUCCEEDED.value),
                        revision=write_int(run.revision),
                        grant=write_text(run.grant_id or ""),
                        one=write_int(1),
                        outcome=write_text(serving_outcome(binding).value),
                        terminal_at=write_text(request.recorded_at),
                    ),
                ),
                "SET #state = :succeeded, revision = revision + :one, "
                "serving_outcome = :outcome, terminal_at = :terminal_at",
            ),
            self._lease_bump(request.claim, request.recorded_at),
            update(
                MemberSpec(
                    "",
                    _serving_head_key(head.domain),
                    _SERVING_HEAD_CHANGED,
                    "version = :version AND #state = :active AND winner_run_id = :run "
                    "AND cohort_revision = :cohort "
                    "AND elected_frontier_digest = :frontier "
                    "AND elected_candidate_digest = :candidates",
                    _values(
                        version=write_int(head.version),
                        active=write_text("ACTIVE"),
                        run=write_text(run.run_id),
                        cohort=write_text(head.cohort_revision),
                        frontier=write_text(head.elected_frontier_digest),
                        candidates=write_text(head.elected_candidate_digest),
                        next_state=write_text("BOUND_DRAINING" if remaining else "IDLE"),
                        one=write_int(1),
                        binding=write_text(binding.binding_id),
                        grant=write_text(binding.grant_id),
                    ),
                ),
                "SET #state = :next_state, version = version + :one, "
                "direct_binding_id = :binding, direct_grant_id = :grant",
            ),
            update(
                MemberSpec(
                    "",
                    _serving_marker_key(run.run_id),
                    NODE_STATE_CHANGED,
                    "#state = :settled AND grant_id = :grant",
                    _values(
                        settled=write_text("SETTLED_ELIGIBLE"),
                        finalized=write_text("FINALIZED"),
                        grant=write_text(run.grant_id or ""),
                        receipt=write_text(request.receipt_digest),
                        at=write_text(request.recorded_at),
                    ),
                ),
                "SET #state = :finalized, receipt_digest = :receipt, finalized_at = :at",
            ),
            self._binding_member(binding, None, "bake_finalize", run.run_id),
            node_transition_member(
                request.node,
                NodeState.SUCCEEDED,
                {
                    "settled_at": write_text(request.recorded_at),
                    "effect_digest": write_text(request.receipt_digest),
                    "effect_record": write_text(request.effect_record),
                },
            ),
            delete(MemberSpec("", _key(due, f"ARM#{run.run_id}"), RUN_STATE_CHANGED)),
            delete(
                MemberSpec(
                    "",
                    _key(due, f"COMPLETE#{run.run_id}"),
                    RUN_STATE_CHANGED,
                    "run_id = :run AND grant_id = :grant "
                    "AND input_effect_roster_digest = :input_digest",
                    _values(
                        run=write_text(run.run_id),
                        grant=write_text(run.grant_id or ""),
                        input_digest=write_text(request.readiness_digest),
                    ),
                )
            ),
            (
                update(
                    MemberSpec(
                        "",
                        cohort_key,
                        _SERVING_HEAD_CHANGED,
                        "winner_run_id = :winner",
                        _values(
                            winner=write_text(run.run_id),
                            remaining=_list_text(remaining),
                            binding=write_text(binding.binding_id),
                        ),
                    ),
                    "SET run_ids = :remaining, direct_binding_id = :binding",
                )
                if remaining
                else delete(
                    MemberSpec(
                        "",
                        cohort_key,
                        _SERVING_HEAD_CHANGED,
                        "winner_run_id = :winner",
                        _values(winner=write_text(run.run_id)),
                    )
                )
            ),
            _precompute_check(binding.grant_id, request.precompute),
        ]
        members.extend(
            check(
                MemberSpec(
                    "",
                    _key(f"RUN#{run.run_id}", f"NODE#{task.name}"),
                    NODE_STATE_CHANGED,
                    "#state = :ok AND effect_digest = :digest AND revision = :rev",
                    _values(
                        ok=write_text(NodeState.SUCCEEDED.value),
                        digest=write_text(effect.effect_digest),
                        rev=write_int(task.revision),
                    ),
                )
            )
            for task, effect in zip(request.tasks, request.effects, strict=True)
        )
        return members

    def _final_image(self, request: BakeFinalizeRequest) -> str:
        """Classify the stored image after an uncertain final transaction."""
        run = self._get(_run_key(request.run.run_id))
        marker = self._get(_serving_marker_key(request.run.run_id))
        binding = self.read_serving_binding(request.binding.grant_id)
        node = self.read_node(request.run.run_id, request.node.name)
        finalized = (
            run is not None
            and _text(run, "state") == RunState.SUCCEEDED.value
            and marker is not None
            and _text(marker, "state") == "FINALIZED"
            and node is not None
            and node.state is NodeState.SUCCEEDED
            and node.effect_digest == request.receipt_digest
            and binding == request.binding
        )
        if finalized:
            return "post"
        untouched = (
            run is not None
            and _text(run, "state") == RunState.GRANTED.value
            and marker is not None
            and _text(marker, "state") == "SETTLED_ELIGIBLE"
            and binding is None
        )
        return "pre" if untouched else "mixed"

    def finalize_bake(self, request: BakeFinalizeRequest) -> BakeFinalizeOutcome:
        """Commit the winner's one final transaction (plan 4.6 under A3; design DB7, DB15).

        Every roster effect and the exact precompute contribution are conditions; only written
        effects add page-pointer members, and only under a binding that authorizes serving
        (ADR-139; any other pointer member is the `GRAPH_BAKE_V3/3` invariant); the FINALIZED
        receipt, the direct binding, the head and
        frontier drain and the run's completion commit together, so no partial cohort is ever
        visible. No member names `GENERATION#`: semantic visibility is publish's (A3). Response
        loss is exact post (already finalized), complete pre (retry once) or mixed (fatal).
        """
        if self._final_image(request) == "post":
            return BakeFinalizeOutcome.ALREADY_FINALIZED
        graph = self._final_members(request)
        pointer = [
            Member(cast("TransactWriteItemTypeDef", item), NODE_STATE_CHANGED)
            for items in request.pointer_items
            for item in items
        ]
        members = [*(self._bind(member) for member in graph), *pointer]
        refuse_generation_members(members)
        written = written_pointer_effects(request.effects)
        # Each written effect is a history put and a CURRENT write, plus the predecessor's
        # expiry when it supersedes one; so the bound is exact when every write supersedes.
        # A binding that does not authorize serving moves no page (ADR-139).
        pointer_effects = len(written) if binding_authorizes_serving(request.binding) else 0
        shaped = (
            len(graph) == _FINAL_AUTHORITY_MEMBERS + len(request.effects)
            and len(request.pointer_items) == pointer_effects
            and all(
                _FIRST_POINTER_MEMBERS <= len(items) <= _SUPERSEDING_POINTER_MEMBERS
                for items in request.pointer_items
            )
            and len(members) <= final_transaction_member_count(request.effects)
        )
        if not shaped:
            raise InvariantError(
                ErrorCode("GRAPH_BAKE_V3", 3),
                "the final transaction is not 10 + N conditions and authority members plus "
                "two or three pointer members per written effect.",
                context={"members": len(members)},
            )
        for _ in range(2):
            try:
                transact(
                    self._client,
                    TransactRequest(
                        members, int(BOUNDS["max_transaction_items"]), refine=_revision_exists
                    ),
                )
            except GraphStoreUnavailableError:
                image = self._final_image(request)
                if image == "post":
                    return BakeFinalizeOutcome.ALREADY_FINALIZED
                if image == "mixed":
                    raise InvariantError(
                        ErrorCode("GRAPH_BAKE_V3", 4),
                        "the final transaction's stored image is neither pre nor post.",
                        context={"signal": DURABLE_BAKE_FLIP_ATOMICITY_INVARIANT},
                    ) from None
                continue
            return BakeFinalizeOutcome.FINALIZED
        raise GraphStoreUnavailableError("the bake final transaction stayed uncertain.")

    def release_finalize_runtime(self, node: NodeRecord) -> None:
        """Delete the finalizer's token and lock after its commit (replayable; DB7 cleanup)."""
        try:
            self._commit(release_members(node))
        except GraphGuardError:
            return


def _revision_exists(failed: tuple[FailedMember, ...]) -> PointerRevisionExistsError | None:
    """Refuse deterministically only when every failed member is an identical history revision.

    ADR-140 amendment 2026-10-28: any other failed member (a moved `CURRENT`, the head, a
    missing predecessor, any graph condition) returns None, so the base guard is raised.
    """
    existing = tuple(existing_history_revision(member.item, member.old) for member in failed)
    if not failed or any(pointer is None for pointer in existing):
        return None
    return PointerRevisionExistsError(cast("tuple[PresetArtifactPointer, ...]", existing))


def refuse_generation_members(members: list[Member]) -> None:
    """A3 (b): refuse any final-transaction member keyed `GENERATION#` before any write."""
    for member in members:
        body = next(iter(cast("dict[str, dict[str, Any]]", member.item).values()))
        key = cast("dict[str, dict[str, str]]", body.get("Key") or body.get("Item") or {})
        partition = key.get("pk") or key.get("partition_key") or {}
        if str(partition.get("S", "")).startswith("GENERATION#"):
            raise InvariantError(
                ErrorCode("GRAPH_BAKE_V3", 5),
                "a bake final transaction may not write a semantic generation pointer.",
                context={"signal": FINALIZER_GENERATION_MEMBER_REFUSED},
            )
