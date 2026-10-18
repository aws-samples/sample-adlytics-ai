# SPDX-License-Identifier: MIT-0
"""One release path, monotonic publication, lineage and downstream binding (slice S4).

`src/adapters/jobs/execution_graph/release.py` is the only grant issuer. This module holds the
pure rules it and its store share:

- the co-release conflict domain. Every ingestion target family connects through the bake
  artifacts that read many datasets, so the ingestion template has one domain; its
  `RELEASE_ORDER#` head serializes grant preparation and its `BLOCK#` record is what decision
  row 6 reads (S1A-8 / R5-5);
- the target reservation set and its proof, and the grant decision: exact pre-grant parity,
  the active domain ordinal, a complete `TARGET_RESERVATION_PROOF` and an unchanged `RUN#`
  revision, or `RELEASE_GRANT_RESERVATION_PROOF_REQUIRED` (BF-1);
- the published-input head `H(U)`: the larger of the graph head and the legacy head, compared
  with the run's retrieval instant at grant time (V-28) and at apply (BF-8), refused with
  `INPUT_REVISION_SUPERSEDED`;
- the legacy head's reduction over a window's newer retrievals (BF-8, legacy head);
- one-step daily lineage; a failed candidate is immutable and repair is refused with
  `LINEAGE_REPAIR_UNSUPPORTED` (BF-4);
- the `POST_GRANT` bodies grant apply writes, bound to the exact grant and served revision, and
  the sibling cohort settlement and serving binding they join into; presentation refuses a
  generation no binding names (`UNGRANTED_GENERATION`).

The S1 contract is pinned while the lagging stacks are accepted (owner-gate Item 14), so S4's
closed sets live here, beside the S3 vocabulary they extend.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, cast

from domain.operations.execution_graph_dispatch import (
    TERMINAL_NODE_STATES,
    NodePhase,
    NodeState,
    ServingCandidate,
    ServingFrontierVector,
    validate_serving_candidates,
)
from domain.operations.generated_execution_graphs import (
    BAKE_TASKS,
    BAKE_UNAVAILABLE_REASONS,
    BAKE_V3,
    BOUNDS,
    TEMPLATES,
)
from domain.operations.run_request import RunState
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "BAKE_BUDGET_EXHAUSTED",
    "BAKE_EFFECT_NAMESPACE",
    "BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND",
    "BAKE_FINALIZE_NODE",
    "BAKE_PLAN_COUNT_MISMATCH",
    "BAKE_REVISION_CURRENT",
    "BAKE_TASK_NODE",
    "BAKE_V3_SCHEMA_VERSION",
    "CONFLICT_DOMAIN_BLOCKED",
    "CONFLICT_UNIT_HISTORY_OVER_BOUND",
    "CONSUMER_EFFECT_NAMESPACE",
    "DETERMINISTIC_INSIGHT_PAGES",
    "DURABLE_BAKE_EFFECT_INVALID",
    "DURABLE_BAKE_FLIP_ATOMICITY_INVARIANT",
    "DURABLE_BAKE_SOURCE_MANIFEST_INVALID",
    "DURABLE_BAKE_SOURCE_PIN_ABSENT",
    "FINALIZER_GENERATION_MEMBER_REFUSED",
    "GRANT_APPLY_NODE",
    "GRANT_PARITY_INCOMPLETE",
    "INGESTION_RELEASE_DOMAIN",
    "INGESTION_TEMPLATE",
    "INPUT_REVISION_SUPERSEDED",
    "INSIGHT_SLOT_UNAVAILABLE",
    "LINEAGE_FAILED_OWNER_REVIEW",
    "LINEAGE_REPAIR_UNSUPPORTED",
    "POST_GRANT_INVARIANT",
    "PRECOMPUTE_BUDGET_EXHAUSTED",
    "RELEASE_GRANT_RESERVATION_PROOF_REQUIRED",
    "RELEASE_ORDER_BUSY",
    "SERVING_PLAN_DIGEST",
    "UNARMED_PRECOMPUTE_IMAGE_MIXED",
    "UNARMED_PRECOMPUTE_REFUSED",
    "UNGRANTED_GENERATION",
    "ApplyDecision",
    "ApplyView",
    "BakeEffectKind",
    "BakeRecoveryReason",
    "BakeRoster",
    "BakeRosterRow",
    "BakeTaskSource",
    "BindingKind",
    "CohortContribution",
    "CohortMember",
    "CohortOutcome",
    "CohortSettlement",
    "DurableBakeEffectInvalidError",
    "DurableBakeEffectV3",
    "DurableBakeSourceManifest",
    "DurableBakeSourceManifestInvalidError",
    "GrantDecision",
    "GrantView",
    "LegacyHistoryOverBoundError",
    "LegacyRetrieval",
    "LineageHead",
    "LineageRepairUnsupportedError",
    "LineageStep",
    "PrecomputeInputEntry",
    "PrecomputeInputV1",
    "ReleaseRefusedError",
    "ReservationSet",
    "ServingBinding",
    "ServingInputFact",
    "ServingOutcome",
    "ServingReuseExpectation",
    "UnarmedPrecomputeRefusedError",
    "UnarmedPrecomputeView",
    "UnavailableClass",
    "UngrantedGenerationError",
    "aggregate_serving_outcome",
    "alias_serving",
    "apply_head_holds",
    "bake_v3_digest",
    "bind_serving",
    "binding_authorizes_serving",
    "cohort_revision",
    "consumer_effect_record",
    "decide_apply",
    "decide_grant",
    "failure_entries",
    "final_transaction_member_count",
    "grant_identity",
    "join_bake_effects",
    "legacy_head",
    "newer_retrievals",
    "next_lineage_step",
    "post_grant_body",
    "pre_grant_parity",
    "published_head",
    "refuse_lineage_repair",
    "release_conflict_domain",
    "require_granted_generation",
    "reservation_set",
    "reservation_targets",
    "run_post_grant_terminal",
    "serving_generation",
    "serving_input_revision",
    "serving_outcome",
    "serving_withheld",
    "settle_cohort",
    "settlement_for",
    "unarmed_precompute_contribution",
    "unavailable_class",
    "unavailable_reason",
    "validate_exact_input_reuse",
    "validate_same_cohort_alias",
    "withheld_bake_contribution",
    "written_pointer_effects",
]

RELEASE_GRANT_RESERVATION_PROOF_REQUIRED: Final = "RELEASE_GRANT_RESERVATION_PROOF_REQUIRED"
"""BF-1: a grant without the active domain ordinal, a complete proof or an unchanged revision."""
INPUT_REVISION_SUPERSEDED: Final = "INPUT_REVISION_SUPERSEDED"
"""BF-8 / V-28: the run's retrieval is older than `H(U)`; no grant, no head move, no seal."""
LINEAGE_REPAIR_UNSUPPORTED: Final = "LINEAGE_REPAIR_UNSUPPORTED"
"""BF-4: a failed candidate is immutable; repair and higher-fence reuse are refused."""
LINEAGE_FAILED_OWNER_REVIEW: Final = "lineage_failed_owner_review"
"""The co-release domain's block reason after a post-grant lineage failure (BF-4)."""
CONFLICT_DOMAIN_BLOCKED: Final = "CONFLICT_DOMAIN_BLOCKED"
"""A domain guard: `BLOCK#<domain>` appeared since the read; re-read (row 6 then refuses)."""
RELEASE_ORDER_BUSY: Final = "RELEASE_ORDER_BUSY"
"""A domain guard: another run holds the domain's active ordinal; the release waits."""
GRANT_PARITY_INCOMPLETE: Final = "GRANT_PARITY_INCOMPLETE"
"""The run's pre-grant nodes have not all succeeded; it is not yet eligible."""
UNGRANTED_GENERATION: Final = "UNGRANTED_GENERATION"
"""Presentation: a graph-authority generation that no serving binding names is not served."""
CONFLICT_UNIT_HISTORY_OVER_BOUND: Final = "conflict_unit_history_over_bound"
"""The legacy-head listing of a window exceeds the generated bound."""
GRANT_APPLY_NODE: Final = "grant_apply"
"""The template node the release issuer settles; it is never sent to an executor."""

_WINDOW_BOUND: Final = int(BOUNDS["max_window_retrievals"])
SERVING_PLAN_DIGEST: Final = canonical_authority_digest(
    [
        "graph-serving-plan/v3",
        "union-precompute",
        40,
        "bake-v3",
        [[row["page"], row["variant"]] for row in BAKE_TASKS["roster"]],
        BAKE_TASKS["lanes"],
        BAKE_TASKS["certified_query_cap"],
    ]
)
"""The reviewed serving plan shape shared by election, workers, and exact-input reuse.

Plan 4.1: the V3 roster bake replaces the falsified `bake-many` outer-four shape, so a binding
produced under the retired plan is never reused as a V3 input."""


class ReleaseRefusedError(Exception):
    """A release command refused with its exact signal; nothing committed."""

    def __init__(self, signal: str) -> None:
        super().__init__(signal)
        self.signal = signal


class LineageRepairUnsupportedError(ReleaseRefusedError):
    """BF-4 (S2 variant): repair or higher-fence reuse of a failed candidate."""

    def __init__(self) -> None:
        super().__init__(LINEAGE_REPAIR_UNSUPPORTED)


class LegacyHistoryOverBoundError(ReleaseRefusedError):
    """The window's retrieval listing is larger than the generated bound."""

    def __init__(self, listed: int) -> None:
        super().__init__(CONFLICT_UNIT_HISTORY_OVER_BOUND)
        self.listed = listed


class UngrantedGenerationError(ReleaseRefusedError):
    """Presentation refuses an artifact generation that no serving binding names."""

    def __init__(self) -> None:
        super().__init__(UNGRANTED_GENERATION)


# --- The co-release conflict domain (S1A-8 / R5-5) ----------------------------------------------


def release_conflict_domain(template: str) -> str:
    """Return the template's one co-release conflict domain.

    The control-plane design connects every pair of target families whose instances may share
    one release roster. Bake artifacts read many datasets, so every ingestion target family is
    in one connected component: the domain is the workload's. This is the conservative choice
    the control-plane design accepts (dynamically disjoint targets serialize grant preparation).
    """
    workload = str(TEMPLATES[template]["workload"])
    return canonical_authority_digest(["release-conflict-domain/v1", workload])


INGESTION_TEMPLATE: Final = "ingestion_manifest_to_served.v1"
INGESTION_RELEASE_DOMAIN: Final = release_conflict_domain(INGESTION_TEMPLATE)
"""The ingestion template's one co-release domain: the key of row 6's `BLOCK#` record."""


# --- Reservations and the grant (BF-1, V-28, V-29) ----------------------------------------------


def pre_grant_parity(nodes: Iterable[tuple[str, NodePhase, NodeState]]) -> bool:
    """Whether every pre-grant node other than `grant_apply` has succeeded (exact parity)."""
    found = [
        state
        for name, phase, state in nodes
        if phase is NodePhase.PRE_GRANT and name != GRANT_APPLY_NODE
    ]
    return bool(found) and all(state is NodeState.SUCCEEDED for state in found)


def reservation_targets(nodes: Iterable[tuple[str, NodePhase, str]]) -> tuple[str, ...]:
    """Return the run's release targets: the physical tables its pre-grant writes hold.

    Canonical order is the UTF-8 byte order of the target, so two plans that share targets
    always reserve them in the same order.
    """
    targets = {
        lock_key
        for name, phase, lock_key in nodes
        if phase is NodePhase.PRE_GRANT and name != GRANT_APPLY_NODE
    }
    return tuple(sorted(targets, key=lambda target: target.encode("utf-8")))


@dataclass(frozen=True, slots=True)
class ReservationSet:
    """One immutable `TARGET_RESERVATION_SET`: the complete target roster at one ordinal."""

    set_id: str
    run_id: str
    domain: str
    ordinal: int
    targets: tuple[str, ...]
    digest: str
    """The ordered digest the proof must reproduce."""


def reservation_set(  # noqa: PLR0913, PLR0917 -- the set's four identities
    run_id: str, domain: str, ordinal: int, targets: Sequence[str]
) -> ReservationSet:
    """Build the immutable reservation set for one run's active ordinal."""
    if not targets:
        raise ReleaseRefusedError(RELEASE_GRANT_RESERVATION_PROOF_REQUIRED)
    ordered = tuple(targets)
    if list(ordered) != sorted(ordered, key=lambda target: target.encode("utf-8")):
        raise ValueError("reservation targets must be in canonical byte order.")
    digest = canonical_authority_digest(
        ["target-reservation-set/v1", domain, ordinal, run_id, list(ordered)]
    )
    set_id = canonical_authority_digest(["target-reservation-set-id/v1", run_id, ordinal])
    return ReservationSet(set_id, run_id, domain, ordinal, ordered, digest)


def grant_identity(run_id: str, ordinal: int) -> str:
    """One grant per run and ordinal; an exact replay re-derives the same identity."""
    return canonical_authority_digest(["release-grant/v1", run_id, ordinal])


def published_head(graph_head: datetime | None, legacy: datetime | None) -> datetime | None:
    """`H(U)`: the larger of the graph head and the legacy head (design.md, "Release")."""
    heads = [head for head in (graph_head, legacy) if head is not None]
    return max(heads) if heads else None


def apply_head_holds(r: datetime, head: datetime | None) -> bool:
    """Whether retrieval `r` may publish over `H(U)`: `r >= H` (BF-8)."""
    return head is None or r >= head


@dataclass(frozen=True, slots=True)
class GrantView:
    """Everything the grant command reads, strongly."""

    run_state: RunState
    grant_id_present: bool
    parity: bool
    """`pre_grant_parity` over the run's nodes."""
    holds_active_ordinal: bool
    """The domain head names this run at the ordinal the command presents."""
    proof_complete: bool
    """A `TARGET_RESERVATION_PROOF` exists with the set's exact count and ordered digest."""
    revision_matches: bool
    """The `RUN#` revision equals the one the command read."""
    blocked: bool
    r: datetime
    head: datetime | None


class GrantDecision(StrEnum):
    """What the grant command does."""

    GRANT = "grant"
    REPLAY = "replay"
    """The run already carries its grant: return it, write nothing."""
    NOT_LIVE = "not_live"
    """The run left OPEN (superseded, fenced, cancelled): no grant, no pointer (V-16)."""
    BLOCKED = "blocked"
    NOT_READY = "not_ready"
    SUPERSEDED = "superseded"
    """`r < H(U)`: fence the run `older_than_current` (V-28)."""
    PROOF_REQUIRED = "proof_required"
    """BF-1: no grant, head move, lineage seal or target mutation."""


def decide_grant(view: GrantView) -> GrantDecision:  # noqa: PLR0911 -- one return per decision
    """Decide one grant command (design.md, "Release" and "The grant marks the run")."""
    if view.grant_id_present:
        return GrantDecision.REPLAY
    if view.run_state is not RunState.OPEN:
        return GrantDecision.NOT_LIVE
    if view.blocked:
        return GrantDecision.BLOCKED
    if not view.parity:
        return GrantDecision.NOT_READY
    if not apply_head_holds(view.r, view.head):
        return GrantDecision.SUPERSEDED
    if not (view.holds_active_ordinal and view.proof_complete and view.revision_matches):
        return GrantDecision.PROOF_REQUIRED
    return GrantDecision.GRANT


@dataclass(frozen=True, slots=True)
class ApplyView:
    """Everything grant apply reads, strongly."""

    run_state: RunState
    run_grant_id: str | None
    grant_id: str
    slot_owned: bool
    r: datetime
    head: datetime | None


class ApplyDecision(StrEnum):
    """What grant apply does."""

    APPLY = "apply"
    ALREADY_APPLIED = "already_applied"
    LINEAGE_FAILURE = "lineage_failure"
    """Defense in depth: a granted run whose apply cannot commit blocks the domain (BF-4)."""


def decide_apply(view: ApplyView) -> ApplyDecision:
    """Decide grant apply; an apply-time refusal after a grant is a post-grant lineage failure."""
    if view.run_state in {RunState.GRANTED, RunState.SUCCEEDED} and (
        view.run_grant_id == view.grant_id
    ):
        return ApplyDecision.ALREADY_APPLIED
    if (
        view.run_state is not RunState.OPEN
        or view.run_grant_id != view.grant_id
        or not view.slot_owned
        or not apply_head_holds(view.r, view.head)
    ):
        return ApplyDecision.LINEAGE_FAILURE
    return ApplyDecision.APPLY


# --- The legacy head (BF-8) ---------------------------------------------------------------------


def newer_retrievals(
    listing: Sequence[tuple[str, datetime]], r: datetime
) -> tuple[tuple[str, datetime], ...]:
    """Return the window's retrievals newer than `r`; only they can raise `H` above `r`.

    The listing is the window prefix's keys with their retrieval instants. One above the
    generated bound is `conflict_unit_history_over_bound`.
    """
    if len(listing) > _WINDOW_BOUND:
        raise LegacyHistoryOverBoundError(len(listing))
    return tuple(sorted((item for item in listing if item[1] > r), key=lambda item: item[1]))


@dataclass(frozen=True, slots=True)
class LegacyRetrieval:
    """One newer retrieval of the window and what legacy authority says about each version."""

    key: str
    r: datetime
    completed_versions: tuple[bool, ...]
    """For each exact version of the key, whether its legacy plan reached COMPLETED."""


def legacy_head(retrievals: Iterable[LegacyRetrieval]) -> datetime | None:
    """Return the largest `r` among the window's retrievals whose legacy plan reached COMPLETED."""
    completed = [item.r for item in retrievals if any(item.completed_versions)]
    return max(completed) if completed else None


# --- Lineage (BF-4) -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineageHead:
    """`LINEAGE#<U>/HEAD`: the last sealed step and whether its candidate failed."""

    sequence: int
    tail_grant_id: str | None
    failed: bool = False


@dataclass(frozen=True, slots=True)
class LineageStep:
    """One immutable one-step append: the candidate grant on its exact predecessor."""

    sequence: int
    predecessor_grant_id: str | None
    candidate_grant_id: str


def next_lineage_step(head: LineageHead, grant_id: str) -> LineageStep:
    """Append one step on a healthy predecessor; a failed candidate refuses every append."""
    if head.failed:
        raise LineageRepairUnsupportedError
    return LineageStep(head.sequence + 1, head.tail_grant_id, grant_id)


def refuse_lineage_repair() -> None:
    """Repair of a failed candidate is not supported in v1 (BF-4, S2 variant)."""
    raise LineageRepairUnsupportedError


# --- Serving revisions and exact-input reuse (coalescing design §2) -----------------------------


@dataclass(frozen=True, slots=True, order=True)
class ServingInputFact:
    """One exact serving-content fact; identity excludes run and grant identifiers."""

    partition: str
    publication_revision: str
    target_digest: str
    prompt_digest: str
    input_digest: str

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.partition,
                self.publication_revision,
                self.target_digest,
                self.prompt_digest,
                self.input_digest,
            )
        ):
            raise ValueError("a serving input fact cannot contain a blank value.")


def cohort_revision(
    domain: str,
    candidates: Sequence[ServingCandidate],
    frontier: ServingFrontierVector,
) -> str:
    """Digest exact roster lineage separately from serving-content identity."""
    if not domain:
        raise ValueError("a cohort revision requires a domain.")
    ordered = validate_serving_candidates(candidates)
    if any(candidate.domain != domain for candidate in ordered):
        raise ValueError("a cohort revision's candidates must match its domain.")
    facts = [
        (
            candidate.run_id,
            candidate.grant_id,
            candidate.grant_ordinal,
            candidate.publication_effect_digest,
        )
        for candidate in ordered
    ]
    return canonical_authority_digest(["graph-serving-cohort/v3", domain, facts, frontier.digest])


def serving_input_revision(
    domain: str,
    facts: Iterable[ServingInputFact],
    declared_plan_digest: str,
) -> str:
    """Digest exact distinct serving inputs while excluding run, grant and roster identity."""
    if not domain or not declared_plan_digest:
        raise ValueError("a serving input revision requires a domain and declared plan digest.")
    ordered = tuple(sorted(set(facts)))
    if not ordered:
        raise ValueError("a serving input revision requires at least one exact input fact.")
    return canonical_authority_digest(
        [
            "graph-serving-input/v3",
            domain,
            [
                (
                    fact.partition,
                    fact.publication_revision,
                    fact.target_digest,
                    fact.prompt_digest,
                    fact.input_digest,
                )
                for fact in ordered
            ],
            declared_plan_digest,
        ]
    )


class BindingKind(StrEnum):
    """A worker-produced binding, or an immutable candidate alias to it."""

    DIRECT = "direct"
    ALIAS = "alias"


@dataclass(frozen=True, slots=True)
class ServingReuseExpectation:
    """The exact latest direct-binding identity eligible for zero-reservation reuse."""

    domain: str
    serving_input_revision: str
    plan_digest: str


# --- POST_GRANT bodies, the cohort and the serving binding --------------------------------------


def post_grant_body(  # noqa: PLR0913 -- the exact binding a POST_GRANT node carries
    *,
    node: str,
    grant_id: str,
    run_id: str,
    conflict_unit: str,
    served_revision: datetime,
    root_flow_id: str,
) -> str:
    """Render a `POST_GRANT` node's owner body, bound to its grant and exact served revision.

    Deterministic, so a retried apply writes byte-identical nodes and their dispatch identity
    stays stable across retries.
    """
    return json.dumps(
        {
            "schema_version": "1",
            "post_grant": node,
            "grant_id": grant_id,
            # The dispatch binding adds `run_id`, so the owner body names the run separately.
            "granted_run": run_id,
            "conflict_unit": conflict_unit,
            "served_revision": served_revision.isoformat(),
            "root_flow_id": root_flow_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class CohortMember(StrEnum):
    """The sibling `POST_GRANT` nodes of one grant."""

    BAKE = "bake"
    PRECOMPUTE = "precompute"


class CohortOutcome(StrEnum):
    """How one sibling settled."""

    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    NO_ELIGIBLE = "no_eligible"


@dataclass(frozen=True, slots=True)
class CohortContribution:
    """One sibling's exact terminal contribution to its grant's cohort.

    S6 §2.8 (D18): `unavailable` lists every artifact the member did not produce, as
    `(artifact_key, reason)` pairs: a `page#variant` for bake, a request key for precompute.
    DB25 additionally binds a precompute contribution to the exact readiness digest and its
    ordered request/no-request roster. Bake contributions never carry those fields.
    """

    grant_id: str
    member: CohortMember
    outcome: CohortOutcome
    artifact_revision: str | None
    settled_at: datetime
    root_flow_id: str
    unavailable: tuple[tuple[str, str], ...] = ()
    input_effect_roster_digest: str | None = None
    request_roster: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        revision = self.artifact_revision is not None
        if self.outcome is CohortOutcome.SUCCEEDED and (not revision or self.unavailable):
            raise ValueError("a succeeded contribution names a revision and nothing unavailable.")
        if self.outcome is CohortOutcome.UNAVAILABLE and not self.unavailable:
            raise ValueError("an unavailable contribution lists what it did not produce.")
        if self.outcome is CohortOutcome.NO_ELIGIBLE and (revision or self.unavailable):
            raise ValueError(
                "a no-eligible contribution names no revision and nothing unavailable."
            )
        if any(not key or not reason for key, reason in self.unavailable):
            raise ValueError("an unavailable artifact names its key and its reason.")
        readiness_bound = self.input_effect_roster_digest is not None or bool(self.request_roster)
        if self.member is not CohortMember.PRECOMPUTE and readiness_bound:
            raise ValueError("only precompute carries readiness-bound contribution fields.")
        if readiness_bound:
            if self.input_effect_roster_digest is None:
                raise ValueError("a readiness-bound contribution names its input digest.")
            _require_digest(self.input_effect_roster_digest, "input_effect_roster_digest")
            if not self.request_roster or len({key for key, _ in self.request_roster}) != len(
                self.request_roster
            ):
                raise ValueError("a readiness-bound contribution has one ordered roster entry.")
            if any(
                not key or disposition not in {"requested", "no_request"}
                for key, disposition in self.request_roster
            ):
                raise ValueError("a precompute roster disposition is not closed.")


BAKE_BUDGET_EXHAUSTED: Final = "bake_budget_exhausted"
"""S6 §2.8 step 5: a planned bake intent its final receive did not reach; nothing written."""
PRECOMPUTE_BUDGET_EXHAUSTED: Final = "precompute_budget_exhausted"
"""S6 §2.8: a precompute request its final receive did not assemble."""
POST_GRANT_INVARIANT: Final = "post_grant_invariant"
"""S6 §2.8: a consumer invariant or a classified pointer-write invariant; alarmed (row 5b)."""
_FAILING_REASONS: Final = frozenset(
    {BAKE_BUDGET_EXHAUSTED, PRECOMPUTE_BUDGET_EXHAUSTED, POST_GRANT_INVARIANT}
)
INSIGHT_SLOT_UNAVAILABLE: Final = "insight_slot_unavailable"
"""A requested insight slot the precompute could not fill; the slot renders its designed state."""


class UnavailableClass(StrEnum):
    """ADR-139: whether one `unavailable` binding entry is a designed absence or a failure."""

    DESIGNED_ABSENCE = "designed_absence"
    FAILURE = "failure"


def unavailable_class(key: str, reason: str) -> UnavailableClass:
    """Classify one `(key, reason)` entry; anything outside the closed set is a failure.

    A page's generated `<page>_materialization_unavailable` is a designed absence only on a key
    whose page is that page. `insight_slot_unavailable` is a designed absence on any key. An
    unknown page, a mismatched page or a `#`-less key fails closed and never raises.
    """
    if reason == INSIGHT_SLOT_UNAVAILABLE:
        return UnavailableClass.DESIGNED_ABSENCE
    page, separator, _variant = key.partition("#")
    if separator and BAKE_UNAVAILABLE_REASONS.get(page) == reason:
        return UnavailableClass.DESIGNED_ABSENCE
    return UnavailableClass.FAILURE


def failure_entries(binding: ServingBinding) -> tuple[tuple[str, str], ...]:
    """Return the binding's `unavailable` entries that are failures, in binding order."""
    return tuple(
        entry
        for entry in binding.unavailable
        if unavailable_class(*entry) is UnavailableClass.FAILURE
    )


def binding_authorizes_serving(binding: ServingBinding) -> bool:
    """ADR-139: the one serving predicate (a "grantable" binding).

    A direct, executed, non-stale binding that names an artifact revision and whose every
    `unavailable` entry is a designed absence. The finalizer gate, the store shape check,
    `require_granted_generation`, the governed-generation probe and ops check O3 all call this.
    """
    return (
        binding.binding_kind is BindingKind.DIRECT
        and binding.execution_attempts > 0
        and not binding.stale
        and binding.artifact_revision is not None
        and not failure_entries(binding)
    )


def serving_withheld(binding: ServingBinding) -> bool:
    """Return whether a finalized binding keeps its pages on last-good because something failed.

    A binding with no revision and no failure entry (every roster row a designed absence) is
    not withheld. A non-authorized binding that names a revision is withheld, so the rule fails
    toward alarming.
    """
    if binding_authorizes_serving(binding):
        return False
    return bool(failure_entries(binding)) or binding.artifact_revision is not None


def settlement_for(contribution: CohortContribution) -> NodeState:
    """S6 §2.8 (D27): a consumer node's outcome, a pure function of its contribution.

    `FAILED` exactly when a listed reason is a budget exhaustion or `post_grant_invariant`,
    `SUCCEEDED` otherwise. The first settlement and every replay call this, so a replay
    always reproduces the original outcome.
    """
    if any(reason in _FAILING_REASONS for _key, reason in contribution.unavailable):
        return NodeState.FAILED
    return NodeState.SUCCEEDED


def serving_generation(grant_id: str) -> str:
    """S6 §2.8: the one artifact revision both consumers of a grant contribute and bind."""
    return canonical_authority_digest(["graph-serving-generation/v1", grant_id])


@dataclass(frozen=True, slots=True)
class CohortSettlement:
    """The durable join of every sibling of one grant, in any arrival order."""

    grant_id: str
    contributions: tuple[CohortContribution, ...]
    settled_at: datetime


def settle_cohort(
    grant_id: str,
    expected: Iterable[CohortMember],
    contributions: Iterable[CohortContribution],
) -> CohortSettlement | None:
    """Join the siblings once every expected member has contributed; None while one is open.

    Arrival order does not matter (precompute before bake or after). A retried contribution
    with the same member and content is the same contribution; a conflicting one is refused.
    """
    members = frozenset(expected)
    found: dict[CohortMember, CohortContribution] = {}
    for item in contributions:
        if item.grant_id != grant_id:
            raise ValueError("a contribution names a different grant.")
        prior = found.get(item.member)
        if prior is not None and prior != item:
            raise ValueError("a sibling contributed two different terminals.")
        found[item.member] = item
    if frozenset(found) != members:
        return None
    ordered = tuple(found[member] for member in sorted(members, key=lambda member: member.value))
    return CohortSettlement(grant_id, ordered, max(item.settled_at for item in ordered))


@dataclass(frozen=True, slots=True)
class ServingBinding:
    """The serving binding: direct worker truth or an immutable alias to direct truth."""

    binding_id: str
    grant_id: str
    conflict_unit: str
    served_revision: datetime
    artifact_revision: str | None
    """None when no sibling produced an artifact (unavailable or no eligible variant)."""
    contributor_roots: tuple[str, ...]
    bound_at: datetime
    unavailable: tuple[tuple[str, str], ...] = ()
    """The union of both contributions' unavailable artifacts, sorted."""
    root_flow_id: str | None = None
    authority_epoch: int | None = None
    domain: str | None = None
    cohort_revision: str | None = None
    serving_input_revision: str | None = None
    plan_digest: str | None = None
    binding_kind: BindingKind | None = None
    """None is legacy read compatibility and is never eligible for exact-input reuse."""
    direct_binding_id: str | None = None
    """An alias names its already-durable direct binding; a direct binding leaves this absent."""
    winner_run_id: str | None = None
    execution_attempts: int = 0
    stale: bool = False

    def __post_init__(self) -> None:
        if self.execution_attempts < 0:
            raise ValueError("serving binding execution attempts cannot be negative.")
        if self.binding_kind is None:
            if any(
                value is not None
                for value in (
                    self.domain,
                    self.cohort_revision,
                    self.serving_input_revision,
                    self.plan_digest,
                    self.direct_binding_id,
                )
            ):
                raise ValueError("a legacy binding cannot carry partial typed binding authority.")
            return
        if any(
            not value
            for value in (
                self.domain,
                self.cohort_revision,
                self.serving_input_revision,
                self.plan_digest,
            )
        ):
            raise ValueError("a typed serving binding requires both revisions and exact identity.")
        if self.binding_kind is BindingKind.DIRECT and self.direct_binding_id is not None:
            raise ValueError("a direct serving binding cannot reference another binding.")
        if self.binding_kind is BindingKind.ALIAS and not self.direct_binding_id:
            raise ValueError("a serving alias must reference an already-durable direct binding.")
        if self.binding_kind is BindingKind.ALIAS and self.artifact_revision is not None:
            raise ValueError("a serving alias cannot carry artifact generation authority.")
        if self.binding_kind is BindingKind.ALIAS and self.execution_attempts:
            raise ValueError("a serving alias cannot claim worker execution attempts.")


class ServingOutcome(StrEnum):
    """What a completed granted run served, aggregated from its direct binding."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NO_ELIGIBLE = "no_eligible"


def serving_outcome(binding: ServingBinding) -> ServingOutcome:
    """Copy the binding's outcome: a revision or none, and something unavailable or nothing."""
    if binding.artifact_revision is not None:
        return ServingOutcome.PARTIAL if binding.unavailable else ServingOutcome.COMPLETE
    return ServingOutcome.UNAVAILABLE if binding.unavailable else ServingOutcome.NO_ELIGIBLE


def validate_exact_input_reuse(
    binding: ServingBinding | None, expectation: ServingReuseExpectation
) -> bool:
    """Allow zero-reservation reuse only from the latest complete executed direct binding."""
    if binding is None:
        return False
    return (
        binding.binding_kind is BindingKind.DIRECT
        and binding.domain == expectation.domain
        and binding.serving_input_revision == expectation.serving_input_revision
        and binding.plan_digest == expectation.plan_digest
        and binding.execution_attempts > 0
        and not binding.stale
        and serving_outcome(binding) is ServingOutcome.COMPLETE
    )


def validate_same_cohort_alias(
    direct: ServingBinding, alias: ServingBinding, candidate: ServingCandidate
) -> bool:
    """Allow loser truth from its exact cohort direct, regardless of the direct outcome."""
    return (
        direct.binding_kind is BindingKind.DIRECT
        and alias.binding_kind is BindingKind.ALIAS
        and alias.direct_binding_id == direct.binding_id
        and alias.grant_id == candidate.grant_id
        and alias.root_flow_id == candidate.root_flow_id
        and direct.domain == alias.domain == candidate.domain
        and direct.cohort_revision == alias.cohort_revision
        and direct.serving_input_revision == alias.serving_input_revision
        and direct.plan_digest == alias.plan_digest
        and direct.winner_run_id == alias.winner_run_id
        and candidate.root_flow_id in direct.contributor_roots
    )


def aggregate_serving_outcome(
    direct: ServingBinding, aliases: Sequence[ServingBinding]
) -> ServingOutcome:
    """Return direct truth only after proving every alias refers to that durable direct binding."""
    if direct.binding_kind is not BindingKind.DIRECT or direct.direct_binding_id is not None:
        raise ValueError("aggregate serving truth requires a direct binding first.")
    if any(
        alias.binding_kind is not BindingKind.ALIAS
        or alias.direct_binding_id != direct.binding_id
        or alias.domain != direct.domain
        for alias in aliases
    ):
        raise ValueError("every serving alias must reference the exact direct binding.")
    return serving_outcome(direct)


def bind_serving(  # noqa: PLR0913 -- the settlement and its exact serving bindings
    settlement: CohortSettlement,
    *,
    conflict_unit: str,
    served_revision: datetime,
    contributor_roots: Iterable[str] = (),
    root_flow_id: str | None = None,
    authority_epoch: int | None = None,
    domain: str | None = None,
    cohort_revision_value: str | None = None,
    serving_input_revision_value: str | None = None,
    plan_digest: str | None = None,
    winner_run_id: str | None = None,
    execution_attempts: int = 0,
) -> ServingBinding:
    """Bind serving from a settled cohort; typed coalescing fields make it directly reusable."""
    revisions = sorted(
        {item.artifact_revision for item in settlement.contributions if item.artifact_revision}
    )
    if len(revisions) > 1:
        raise ValueError("one serving binding names exactly one artifact revision.")
    roots = sorted({*contributor_roots, *(item.root_flow_id for item in settlement.contributions)})
    artifact = revisions[0] if revisions else None
    binding_identity: list[object]
    if domain is None:
        binding_identity = ["serving-binding/v1", settlement.grant_id, artifact or "", roots]
    else:
        binding_identity = [
            "serving-binding/v3",
            settlement.grant_id,
            artifact or "",
            roots,
            domain,
            cohort_revision_value or "",
            serving_input_revision_value or "",
            plan_digest or "",
        ]
    binding_id = canonical_authority_digest(binding_identity)
    unavailable = tuple(
        sorted(
            {item for contribution in settlement.contributions for item in contribution.unavailable}
        )
    )
    return ServingBinding(
        binding_id=binding_id,
        grant_id=settlement.grant_id,
        conflict_unit=conflict_unit,
        served_revision=served_revision,
        artifact_revision=artifact,
        contributor_roots=tuple(roots),
        bound_at=settlement.settled_at,
        unavailable=unavailable,
        root_flow_id=root_flow_id,
        authority_epoch=authority_epoch,
        domain=domain,
        cohort_revision=cohort_revision_value,
        serving_input_revision=serving_input_revision_value,
        plan_digest=plan_digest,
        binding_kind=BindingKind.DIRECT if domain is not None else None,
        winner_run_id=winner_run_id,
        execution_attempts=execution_attempts,
    )


def alias_serving(  # noqa: PLR0913 -- exact alias authority
    direct: ServingBinding,
    candidate: ServingCandidate,
    cohort_revision_value: str,
    *,
    bound_at: datetime,
) -> ServingBinding:
    """Create immutable loser or exact-input-reuse truth against one proven direct binding."""
    if direct.binding_kind is not BindingKind.DIRECT or direct.domain != candidate.domain:
        raise ValueError("a serving alias requires a direct binding in the candidate domain.")
    if direct.serving_input_revision is None or direct.plan_digest is None:
        raise ValueError("a serving alias requires complete typed direct authority.")
    binding_id = canonical_authority_digest(
        [
            "serving-alias/v3",
            candidate.grant_id,
            direct.binding_id,
            cohort_revision_value,
            direct.serving_input_revision,
        ]
    )
    return ServingBinding(
        binding_id=binding_id,
        grant_id=candidate.grant_id,
        conflict_unit=direct.conflict_unit,
        served_revision=direct.served_revision,
        artifact_revision=None,
        contributor_roots=(candidate.root_flow_id,),
        bound_at=bound_at,
        root_flow_id=candidate.root_flow_id,
        authority_epoch=direct.authority_epoch,
        domain=candidate.domain,
        cohort_revision=cohort_revision_value,
        serving_input_revision=direct.serving_input_revision,
        plan_digest=direct.plan_digest,
        binding_kind=BindingKind.ALIAS,
        direct_binding_id=direct.binding_id,
        winner_run_id=direct.winner_run_id,
    )


def require_granted_generation(
    bindings: Mapping[str, ServingBinding], artifact_revision: str, *, graph_authority: bool
) -> ServingBinding | None:
    """Return the binding that names the revision; refuse a graph generation none names.

    A legacy-authority artifact (before cutover) has no grant and is served as before. Historical
    graph bindings remain readable until writer migration, while typed aliases never authorize a
    generation. A typed direct binding serves only when `binding_authorizes_serving` holds:
    executed, non-stale truth whose every unavailable entry is a designed absence (ADR-139).
    """
    if not graph_authority:
        return None
    for binding in bindings.values():
        if binding.artifact_revision != artifact_revision:
            continue
        if binding.binding_kind is None:
            return binding
        if binding_authorizes_serving(binding):
            return binding
    raise UngrantedGenerationError


def run_post_grant_terminal(states: Sequence[NodeState]) -> bool:
    """Whether every POST_GRANT node of a granted run is terminal (the run may then succeed)."""
    return all(state in TERMINAL_NODE_STATES for state in states)


# --- GRAPH bake V3 (plan 4.1; design DB20-DB23, A1.1, A1.2, F8, F9, F12) -----------------------

BAKE_V3_SCHEMA_VERSION: Final = int(BAKE_V3["schema_version"])
BAKE_IDENTIFIER_MAX: Final = int(BAKE_V3["identifier_max_length"])
BAKE_REVISION_CURRENT: Final = str(BAKE_V3["unchanged_reason"])
"""The one reason an `unchanged` effect carries: the source computed the current revision."""
BAKE_EFFECT_NAMESPACE: Final = "graph-durable-bake-effect/v3"
BAKE_SOURCE_MANIFEST_NAMESPACE: Final = "graph-durable-bake-source-manifest/v1"
BAKE_TASK_SOURCE_NAMESPACE: Final = "graph-durable-bake-task-source/v1"
BAKE_PLAN_COUNTS_NAMESPACE: Final = "graph-durable-bake-plan-counts/v1"
BAKE_ROSTER_NAMESPACE: Final = "graph-durable-bake-roster/v1"
PRECOMPUTE_INPUT_NAMESPACE: Final = "graph-precompute-input/v1"
DURABLE_BAKE_EFFECT_INVALID: Final = "durable_bake_effect_invalid"
DURABLE_BAKE_SOURCE_MANIFEST_INVALID: Final = "durable_bake_source_manifest_invalid"
DURABLE_BAKE_SOURCE_PIN_ABSENT: Final = "durable_bake_source_pin_absent"
DURABLE_BAKE_FLIP_ATOMICITY_INVARIANT: Final = "durable_bake_flip_atomicity_invariant"
BAKE_PLAN_COUNT_MISMATCH: Final = "bake_plan_count_mismatch"
BAKE_EXECUTIONS_OVER_CERTIFIED_BOUND: Final = "bake_executions_over_certified_bound"
"""A task render asked for more distinct requests than its row's certified bound (A5.1 I5)."""
FINALIZER_GENERATION_MEMBER_REFUSED: Final = "finalizer_generation_member_refused"
BAKE_TASK_NODE: Final = "bake_task"
BAKE_FINALIZE_NODE: Final = "bake_finalize"
_V3_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_V3_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")
"""A roster page: hyphen-only, so its `-` -> `_` reason normalization stays collision-free."""
_V3_VARIANT_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
"""A roster variant (A5.2, O1): the domain variant grammar; calendar presets carry `_`."""


class BakeEffectKind(StrEnum):
    """DB22: the closed effect kinds of one roster task."""

    WRITTEN = "written"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"


class BakeRecoveryReason(StrEnum):
    """F9: why a task's `BAKE_RECOVERY#` obligation exists (contract `recovery_reasons`)."""

    FATAL = "fatal"
    RETRY_EXHAUSTED = "retry_exhausted"


if tuple(kind.value for kind in BakeEffectKind) != tuple(BAKE_V3["effect_kinds"]) or tuple(
    reason.value for reason in BakeRecoveryReason
) != tuple(BAKE_V3["recovery_reasons"]):
    raise RuntimeError("the V3 enums drifted from the generated bake_v3 contract block.")


class DurableBakeEffectInvalidError(InvariantError):
    """DB22: a V3 effect breaks the closed field-presence or page-reason rules; fatal."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            ErrorCode("GRAPH_BAKE_V3", 1),
            "a durable bake effect is not a valid closed V3 effect.",
            context={"signal": DURABLE_BAKE_EFFECT_INVALID, "detail": detail},
        )
        self.signal = DURABLE_BAKE_EFFECT_INVALID
        self.detail = detail


class DurableBakeSourceManifestInvalidError(InvariantError):
    """DB21: a pinned manifest is malformed, misaligned or digest-mismatched; fatal."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            ErrorCode("GRAPH_BAKE_V3", 2),
            "a durable bake source manifest is not the exact pinned manifest.",
            context={"signal": DURABLE_BAKE_SOURCE_MANIFEST_INVALID, "detail": detail},
        )
        self.signal = DURABLE_BAKE_SOURCE_MANIFEST_INVALID
        self.detail = detail


def bake_v3_digest(namespace: str, value: object) -> str:
    """Return `sha256:` plus the 64-hex canonical digest of one namespaced V3 value."""
    return f"sha256:{canonical_authority_digest([namespace, value])}"


def _require_digest(value: str | None, name: str) -> None:
    if value is None or not _V3_DIGEST.fullmatch(value):
        raise DurableBakeEffectInvalidError(f"{name} is not a sha256: digest")


def _require_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        or not value.strip()
        or len(value) > BAKE_IDENTIFIER_MAX
    ):
        raise DurableBakeEffectInvalidError(f"{name} is not a nonblank identifier of <= 80")


def unavailable_reason(page: str) -> str:
    """Return a roster page's one generated terminal unavailable reason (F12)."""
    try:
        return str(BAKE_UNAVAILABLE_REASONS[page])
    except KeyError:
        raise DurableBakeEffectInvalidError(f"page {page} has no generated reason") from None


@dataclass(frozen=True, slots=True)
class BakeRosterRow:
    """One roster task: page, variant, budget, write seconds, horizon and certified count.

    The budget and write seconds are A1.1's; the source horizon bounds every whole-scope read the
    row pins (A5.1 I9); the certified plan count bounds the distinct requests one render executes
    (A5.1 I5). A row without a valid count can't be constructed, so no task runs unbounded.
    """

    page: str
    variant: str
    budget_seconds: float
    write_seconds: float
    source_horizon_days: int
    certified_plan_count: int

    def __post_init__(self) -> None:
        for name, value, grammar in (
            ("page", self.page, _V3_IDENTIFIER),
            ("variant", self.variant, _V3_VARIANT_IDENTIFIER),
        ):
            if not grammar.fullmatch(value) or len(value) > BAKE_IDENTIFIER_MAX:
                raise ValueError(f"a bake roster {name} is not a lowercase identifier.")
        if self.budget_seconds <= 0 or self.write_seconds <= 0:
            raise ValueError("a bake roster row's budget and write seconds are positive.")
        if (
            isinstance(self.source_horizon_days, bool)  # pyright: ignore[reportUnnecessaryIsInstance]
            or not isinstance(self.source_horizon_days, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or self.source_horizon_days < 1
        ):
            raise ValueError(
                "BAKE_SOURCE_HORIZON_DAYS_INVALID: a bake roster row's source horizon is an "
                "integer number of days, at least one."
            )
        if (
            isinstance(self.certified_plan_count, bool)  # pyright: ignore[reportUnnecessaryIsInstance]
            or not isinstance(self.certified_plan_count, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or self.certified_plan_count < 1
        ):
            raise ValueError(
                "BAKE_CERTIFIED_PLAN_COUNTS_INVALID: a bake roster row's certified plan count is "
                "an integer, at least one."
            )

    @property
    def key(self) -> str:
        """The row's `<page>#<variant>` key, the contract budget key."""
        return f"{self.page}#{self.variant}"


@dataclass(frozen=True, slots=True)
class BakeRoster:
    """The composition-built roster every V3 consumer takes as an argument (A1.1, A1.5, F8).

    N is data: no consumer counts rows itself. Construction refuses an empty roster, a
    duplicate row and N above `BAKE_TASKS.max_tasks` (the finalization transaction bound).
    """

    rows: tuple[BakeRosterRow, ...]

    def __post_init__(self) -> None:
        if not self.rows:
            raise ValueError("a bake roster has at least one row.")
        keys = [row.key for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("a bake roster repeats a row.")
        if len(self.rows) > int(BAKE_TASKS["max_tasks"]):
            raise ValueError("a bake roster exceeds BAKE_TASKS.max_tasks.")

    @classmethod
    def from_contract(cls) -> BakeRoster:
        """Build the roster once from the generated `BAKE_TASKS` (composition only)."""
        budgets = BAKE_TASKS["budget_seconds"]
        writes = BAKE_TASKS["write_seconds"]
        horizons = cast("Mapping[str, int]", BAKE_TASKS.get("source_horizon_days", {}))
        certified = cast("Mapping[str, int]", BAKE_TASKS.get("certified_plan_counts", {}))
        return cls(
            tuple(
                BakeRosterRow(
                    str(row["page"]),
                    str(row["variant"]),
                    float(budgets[f"{row['page']}#{row['variant']}"]),
                    float(writes[row["page"]]),
                    horizons.get(f"{row['page']}#{row['variant']}", 0),
                    certified.get(f"{row['page']}#{row['variant']}", 0),
                )
                for row in BAKE_TASKS["roster"]
            )
        )

    @property
    def keys(self) -> tuple[str, ...]:
        """Every row's key, in roster order."""
        return tuple(row.key for row in self.rows)

    @property
    def pages(self) -> tuple[str, ...]:
        """The roster's pages, each once, in first-row order (A1.2)."""
        return tuple(dict.fromkeys(row.page for row in self.rows))

    @property
    def digest(self) -> str:
        """The roster's canonical digest over its ordered rows."""
        return bake_v3_digest(
            BAKE_ROSTER_NAMESPACE,
            [
                [
                    row.page,
                    row.variant,
                    row.budget_seconds,
                    row.write_seconds,
                    row.certified_plan_count,
                    row.source_horizon_days,
                ]
                for row in self.rows
            ],
        )

    def contains(self, key: str) -> bool:
        """Whether `key` is a roster row; a declared page with no bake source is not (ADR-144)."""
        return key in self.keys

    def row(self, key: str) -> BakeRosterRow:
        """Return the row with `key`; a key outside the roster is an invariant."""
        for item in self.rows:
            if item.key == key:
                return item
        raise DurableBakeEffectInvalidError(f"{key} is not a roster row")


@dataclass(frozen=True, slots=True)
class BakeTaskSource:
    """One roster task's pinned source slice (DB21, S5).

    `reads_digest` covers every exact publication read the task's source made at fan-out;
    `request_digests` are its ordered complete-bound query requests, and `plan_count` their
    distinct count after exact fan-in (4.4) - the true post-fan-in plan count.
    """

    page: str
    variant: str
    reads_digest: str
    request_digests: tuple[str, ...]
    plan_count: int

    def __post_init__(self) -> None:
        _require_digest(self.reads_digest, "reads_digest")
        for digest in self.request_digests:
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise DurableBakeSourceManifestInvalidError("a request digest is malformed")
        if (
            isinstance(self.plan_count, bool)  # pyright: ignore[reportUnnecessaryIsInstance]
            or self.plan_count < 0
            or self.plan_count > len(self.request_digests)
            or (self.plan_count == 0) != (not self.request_digests)
        ):
            raise DurableBakeSourceManifestInvalidError("a plan count disagrees with its requests")

    @property
    def key(self) -> str:
        """The task's `<page>#<variant>` key."""
        return f"{self.page}#{self.variant}"

    @property
    def digest(self) -> str:
        """The slice's canonical digest; the V3 effect's `source_manifest_digest`."""
        return bake_v3_digest(
            BAKE_TASK_SOURCE_NAMESPACE,
            [
                self.page,
                self.variant,
                self.reads_digest,
                list(self.request_digests),
                self.plan_count,
            ],
        )


@dataclass(frozen=True, slots=True)
class DurableBakeSourceManifest:
    """The winner's immutable source pin: one slice per roster row, in roster order (S5).

    It carries the N pinned plan counts and their digest; `two_lane_partition` runs on these
    counts only after the pin commits.
    """

    run_id: str
    grant_id: str
    roster_digest: str
    tasks: tuple[BakeTaskSource, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, "run_id")
        _require_identifier(self.grant_id, "grant_id")
        _require_digest(self.roster_digest, "roster_digest")
        if not self.tasks:
            raise DurableBakeSourceManifestInvalidError("a manifest pins at least one task")

    def aligned_to(self, roster: BakeRoster) -> DurableBakeSourceManifest:
        """Return self when it pins exactly `roster`'s rows in roster order, else refuse."""
        if (
            self.roster_digest != roster.digest
            or tuple(task.key for task in self.tasks) != roster.keys
        ):
            raise DurableBakeSourceManifestInvalidError("the manifest does not pin this roster")
        return self

    @property
    def plan_counts(self) -> tuple[int, ...]:
        """The N pinned plan counts, in roster order."""
        return tuple(task.plan_count for task in self.tasks)

    @property
    def plan_counts_digest(self) -> str:
        """The digest of the N pinned counts, persisted with the pin."""
        return bake_v3_digest(
            BAKE_PLAN_COUNTS_NAMESPACE, [[task.key, task.plan_count] for task in self.tasks]
        )

    @property
    def digest(self) -> str:
        """The manifest digest the pin stores and every task re-checks."""
        return bake_v3_digest(
            BAKE_SOURCE_MANIFEST_NAMESPACE,
            [self.run_id, self.grant_id, self.roster_digest, [t.digest for t in self.tasks]],
        )

    def task(self, key: str) -> BakeTaskSource:
        """Return the pinned slice of `key`, or refuse a key the manifest does not pin."""
        for item in self.tasks:
            if item.key == key:
                return item
        raise DurableBakeSourceManifestInvalidError(f"{key} is not pinned")


@dataclass(frozen=True, slots=True)
class DurableBakeEffectV3:
    """DB22: one roster task's closed terminal effect.

    | kind          | reason                          | artifact | output / pointer |
    |---------------|---------------------------------|----------|------------------|
    | `written`     | none                            | required | both required    |
    | `unchanged`   | `revision_current`              | required | both absent      |
    | `unavailable` | the page's generated reason     | absent   | both absent      |

    Every kind requires `source_manifest_digest`. Construction refuses anything else.
    """

    task_id: str
    run_id: str
    grant_id: str
    head_version: int
    page: str
    variant: str
    effect_kind: BakeEffectKind
    reason_code: str | None
    source_manifest_digest: str
    artifact_revision: str | None
    output_manifest_digest: str | None
    pointer_candidate_digest: str | None
    schema_version: int = BAKE_V3_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BAKE_V3_SCHEMA_VERSION:
            raise DurableBakeEffectInvalidError("schema_version is not 3")
        for name in ("task_id", "run_id", "grant_id", "page", "variant"):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.effect_kind, BakeEffectKind):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise DurableBakeEffectInvalidError("effect_kind is not a closed kind")
        if isinstance(self.head_version, bool) or self.head_version < 0:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise DurableBakeEffectInvalidError("head_version is not a counter")
        _require_digest(self.source_manifest_digest, "source_manifest_digest")
        self._require_presence()

    def _require_presence(self) -> None:
        kind = self.effect_kind
        artifact = self.artifact_revision is not None
        outputs = (self.output_manifest_digest, self.pointer_candidate_digest)
        if kind is BakeEffectKind.WRITTEN:
            if self.reason_code is not None or not artifact or None in outputs:
                raise DurableBakeEffectInvalidError("written needs artifact and outputs only")
            for name, value in zip(("output", "pointer"), outputs, strict=True):
                _require_digest(value, name)
        elif kind is BakeEffectKind.UNCHANGED:
            if self.reason_code != BAKE_REVISION_CURRENT or not artifact or outputs != (None, None):
                raise DurableBakeEffectInvalidError("unchanged is revision_current, no outputs")
        elif (
            self.reason_code != unavailable_reason(self.page)
            or artifact
            or outputs
            != (
                None,
                None,
            )
        ):
            raise DurableBakeEffectInvalidError("unavailable carries only its page's reason")

    @property
    def key(self) -> str:
        """The effect's `<page>#<variant>` roster key."""
        return f"{self.page}#{self.variant}"

    def to_record(self) -> dict[str, object]:
        """Return the closed record (every field, `effect_digest` excluded)."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "grant_id": self.grant_id,
            "head_version": self.head_version,
            "page": self.page,
            "variant": self.variant,
            "effect_kind": self.effect_kind.value,
            "reason_code": self.reason_code,
            "source_manifest_digest": self.source_manifest_digest,
            "artifact_revision": self.artifact_revision,
            "output_manifest_digest": self.output_manifest_digest,
            "pointer_candidate_digest": self.pointer_candidate_digest,
        }

    @property
    def effect_digest(self) -> str:
        """`SHA256(graph-durable-bake-effect/v3, canonical fields except effect_digest)`."""
        return bake_v3_digest(BAKE_EFFECT_NAMESPACE, self.to_record())

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> DurableBakeEffectV3:
        """Parse a stored record; an unknown or missing field is fatal before any use."""
        fields = set(record)
        expected = {
            "schema_version",
            "task_id",
            "run_id",
            "grant_id",
            "head_version",
            "page",
            "variant",
            "effect_kind",
            "reason_code",
            "source_manifest_digest",
            "artifact_revision",
            "output_manifest_digest",
            "pointer_candidate_digest",
        }
        if fields != expected:
            raise DurableBakeEffectInvalidError("the record's fields are not the closed V3 set")
        try:
            kind = BakeEffectKind(str(record["effect_kind"]))
        except ValueError:
            raise DurableBakeEffectInvalidError("effect_kind is unknown") from None

        def text(name: str) -> str | None:
            value = record[name]
            if value is not None and not isinstance(value, str):
                raise DurableBakeEffectInvalidError(f"{name} is not text")
            return value

        version, head = record["schema_version"], record["head_version"]
        if not isinstance(version, int) or not isinstance(head, int):
            raise DurableBakeEffectInvalidError("a counter field is not an integer")
        return cls(
            task_id=str(text("task_id")),
            run_id=str(text("run_id")),
            grant_id=str(text("grant_id")),
            head_version=head,
            page=str(text("page")),
            variant=str(text("variant")),
            effect_kind=kind,
            reason_code=text("reason_code"),
            source_manifest_digest=str(text("source_manifest_digest")),
            artifact_revision=text("artifact_revision"),
            output_manifest_digest=text("output_manifest_digest"),
            pointer_candidate_digest=text("pointer_candidate_digest"),
            schema_version=version,
        )


def join_bake_effects(
    roster: BakeRoster, effects: Iterable[DurableBakeEffectV3]
) -> tuple[DurableBakeEffectV3, ...]:
    """Join effects in roster (identity) order, never completion order (A1.5, F8).

    Every roster row needs exactly one effect; a missing, duplicate or extra effect refuses the
    whole join, so a nine-row roster can never finalize on seven effects. Mixed written,
    unchanged and unavailable effects are a valid join.
    """
    by_key: dict[str, DurableBakeEffectV3] = {}
    for effect in effects:
        if effect.key in by_key:
            raise DurableBakeEffectInvalidError(f"{effect.key} has two effects")
        by_key[effect.key] = effect
    if set(by_key) != set(roster.keys):
        raise DurableBakeEffectInvalidError(
            f"the effects cover {len(by_key)} of the roster's {len(roster.rows)} rows"
        )
    return tuple(by_key[key] for key in roster.keys)


def written_pointer_effects(
    effects: Sequence[DurableBakeEffectV3],
) -> tuple[DurableBakeEffectV3, ...]:
    """Project the effects that contribute pointer members: `written` only (DB22, DB24)."""
    return tuple(effect for effect in effects if effect.effect_kind is BakeEffectKind.WRITTEN)


def final_transaction_member_count(effects: Sequence[DurableBakeEffectV3]) -> int:
    """Bound the rolling V3 final transaction: `10 + N + 3 x written`, from generated terms.

    `final_authority_members` immutable-cohort authority members, `final_task_condition_members`
    exact conditions per roster effect, and at most `final_written_pointer_members` pointer
    members per written effect (history put, CURRENT write, predecessor expiry). The bound is
    exact when every written effect supersedes a predecessor (A5.3).
    """
    return (
        int(BAKE_TASKS["final_authority_members"])
        + int(BAKE_TASKS["final_task_condition_members"]) * len(effects)
        + int(BAKE_TASKS["final_written_pointer_members"]) * len(written_pointer_effects(effects))
    )


DETERMINISTIC_INSIGHT_PAGES: Final = frozenset(
    {"scorecard", "new-customers", "reach", "retention", "audiences"}
)
"""O5 (B3): the roster pages whose insight slots are deterministic by contract (their contracts
declare no model prompt) and baked into their bodies. Precompute attests each body and asks no
model for it; mandatory real-Bedrock acceptance covers campaigns and retail-health."""


@dataclass(frozen=True, slots=True)
class PrecomputeInputEntry:
    """One roster effect as precompute input: an attested artifact or typed no-request."""

    page: str
    variant: str
    effect_kind: BakeEffectKind
    source_manifest_digest: str
    artifact_revision: str | None
    output_manifest_digest: str | None
    reason_code: str | None
    body_key: str | None = None
    body_digest: str | None = None
    custody_digest: str | None = None

    @property
    def key(self) -> str:
        """Return this input's roster identity."""
        return f"{self.page}#{self.variant}"

    def __post_init__(self) -> None:
        _require_digest(self.source_manifest_digest, "source_manifest_digest")
        available = self.effect_kind is not BakeEffectKind.UNAVAILABLE
        manifest = (self.body_key, self.body_digest, self.custody_digest)
        if available and self.artifact_revision is None:
            raise ValueError("an available precompute input names its artifact revision.")
        if not available and (self.artifact_revision is not None or any(manifest)):
            raise ValueError("an unavailable precompute input carries no artifact manifest.")
        if any(manifest) and None in manifest[:2]:
            raise ValueError("an artifact precompute input carries the complete manifest.")
        if (
            self.body_key is not None
            and self.custody_digest is None
            and self.page not in DETERMINISTIC_INSIGHT_PAGES
        ):
            raise ValueError("an artifact precompute input carries the complete manifest.")
        if self.body_digest is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", self.body_digest) or (
                self.custody_digest is not None
                and not re.fullmatch(r"[0-9a-f]{64}", self.custody_digest)
            ):
                raise ValueError("precompute body/custody checksums are malformed.")
            expected = f"pages/{self.page}/{self.variant}/{self.artifact_revision}.json"
            if self.body_key != expected:
                raise ValueError("a precompute body key does not match its immutable identity.")


@dataclass(frozen=True, slots=True)
class PrecomputeInputV1:
    """DB25: the ordered N-entry precompute input built from one effect join."""

    entries: tuple[PrecomputeInputEntry, ...]

    def __post_init__(self) -> None:
        if not self.entries or len({entry.key for entry in self.entries}) != len(self.entries):
            raise ValueError("a precompute input has one entry per roster row.")

    @classmethod
    def from_effects(cls, effects: Sequence[DurableBakeEffectV3]) -> PrecomputeInputV1:
        """Project each joined effect into its precompute entry, preserving roster order."""
        return cls(
            tuple(
                PrecomputeInputEntry(
                    effect.page,
                    effect.variant,
                    effect.effect_kind,
                    effect.source_manifest_digest,
                    effect.artifact_revision,
                    effect.output_manifest_digest,
                    effect.reason_code,
                )
                for effect in effects
            )
        )

    @classmethod
    def from_effects_and_manifests(
        cls,
        effects: Sequence[DurableBakeEffectV3],
        manifests: Mapping[str, tuple[str, str, str | None]],
    ) -> PrecomputeInputV1:
        """Bind each available effect to its exact immutable body and custody manifest.

        Only a deterministic-insight page (O5), which bakes no custody manifest, binds its body
        alone; every other available effect needs its custody digest.
        """
        expected = {
            effect.key for effect in effects if effect.effect_kind is not BakeEffectKind.UNAVAILABLE
        }
        if set(manifests) != expected:
            raise ValueError("precompute manifests do not cover the available effect roster.")
        if any(
            manifests[effect.key][2] is None and effect.page not in DETERMINISTIC_INSIGHT_PAGES
            for effect in effects
            if effect.key in manifests
        ):
            raise ValueError("an available model-insight effect has no custody manifest.")
        return cls(
            tuple(
                PrecomputeInputEntry(
                    effect.page,
                    effect.variant,
                    effect.effect_kind,
                    effect.source_manifest_digest,
                    effect.artifact_revision,
                    effect.output_manifest_digest,
                    effect.reason_code,
                    *(manifests.get(effect.key, (None, None, None))),
                )
                for effect in effects
            )
        )

    def to_json(self) -> str:
        """Return the canonical stored readiness input."""
        return json.dumps(
            [
                {
                    "page": entry.page,
                    "variant": entry.variant,
                    "effect_kind": entry.effect_kind.value,
                    "source_manifest_digest": entry.source_manifest_digest,
                    "artifact_revision": entry.artifact_revision,
                    "output_manifest_digest": entry.output_manifest_digest,
                    "reason_code": entry.reason_code,
                    "body_key": entry.body_key,
                    "body_digest": entry.body_digest,
                    "custody_digest": entry.custody_digest,
                }
                for entry in self.entries
            ],
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> PrecomputeInputV1:
        """Parse the exact stored readiness input and reject field or type drift."""
        decoded: object = json.loads(text)
        if not isinstance(decoded, list):
            raise ValueError("a stored precompute input is not a roster.")
        expected = {
            "page",
            "variant",
            "effect_kind",
            "source_manifest_digest",
            "artifact_revision",
            "output_manifest_digest",
            "reason_code",
            "body_key",
            "body_digest",
            "custody_digest",
        }
        entries: list[PrecomputeInputEntry] = []
        for value in cast("list[object]", decoded):
            if not isinstance(value, dict) or set(cast("dict[str, object]", value)) != expected:
                raise ValueError("a stored precompute input entry is not closed.")
            item = cast("dict[str, object]", value)
            entries.append(
                PrecomputeInputEntry(
                    page=str(item["page"]),
                    variant=str(item["variant"]),
                    effect_kind=BakeEffectKind(str(item["effect_kind"])),
                    source_manifest_digest=str(item["source_manifest_digest"]),
                    artifact_revision=(
                        None
                        if item["artifact_revision"] is None
                        else str(item["artifact_revision"])
                    ),
                    output_manifest_digest=(
                        None
                        if item["output_manifest_digest"] is None
                        else str(item["output_manifest_digest"])
                    ),
                    reason_code=(None if item["reason_code"] is None else str(item["reason_code"])),
                    body_key=None if item["body_key"] is None else str(item["body_key"]),
                    body_digest=(None if item["body_digest"] is None else str(item["body_digest"])),
                    custody_digest=(
                        None if item["custody_digest"] is None else str(item["custody_digest"])
                    ),
                )
            )
        parsed = cls(tuple(entries))
        if parsed.to_json() != text:
            raise ValueError("a stored precompute input is not canonical.")
        return parsed

    @property
    def digest(self) -> str:
        """The input's canonical digest (`input_effect_roster_digest`)."""
        return bake_v3_digest(PRECOMPUTE_INPUT_NAMESPACE, self.to_json())


# --- ADR-140: a bake that skips the finalizer still settles its cohort --------------------------

CONSUMER_EFFECT_NAMESPACE: Final = "execution-graph-post-grant-effect/v1"
"""The namespace of a settled POST_GRANT consumer node's effect record (S6 §2.8)."""
UNARMED_PRECOMPUTE_REFUSED: Final = ErrorCode("GRAPH_PRECOMPUTE", 7)
"""ADR-140: a winner whose bake skipped the finalizer is in a shape outside U1/U2 (B8)."""
UNARMED_PRECOMPUTE_IMAGE_MIXED: Final = ErrorCode("GRAPH_PRECOMPUTE", 6)
"""ADR-140: the unarmed settlement's stored image after a lost response is neither pre nor post."""
_FAN_OUT_PREFIX: Final = f"{BAKE_TASK_NODE}:"


def consumer_effect_record(
    member: CohortMember, contribution: CohortContribution, counts: Mapping[str, int]
) -> tuple[str, str]:
    """Return a consumer node's canonical effect record and its digest (S6 §2.8, D27).

    The POST_GRANT consumer settles every contributing node with this record; the Release's
    unarmed settlement writes the same bytes, so a Release-settled precompute node is
    indistinguishable from a consumer-settled `NO_ELIGIBLE` one.
    """
    record = json.dumps(
        {
            "schema_version": "1",
            "node": member.value,
            "grant_id": contribution.grant_id,
            "outcome": contribution.outcome.value,
            "artifact_revision": contribution.artifact_revision,
            "unavailable": [list(item) for item in contribution.unavailable],
            **dict(counts),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return record, canonical_authority_digest([CONSUMER_EFFECT_NAMESPACE, record])


def withheld_bake_contribution(  # noqa: PLR0913, PLR0917 -- the grant, root, keys and instant
    grant_id: str, root_flow_id: str, keys: Sequence[str], at: datetime
) -> CohortContribution:
    """ADR-140 (Z3): the bake member of a cohort whose stored precompute is stale.

    `UNAVAILABLE`, no revision, every roster key `post_grant_invariant`, so the binding is withheld
    (`serving_withheld`) and moves no page pointer.
    """
    return CohortContribution(
        grant_id,
        CohortMember.BAKE,
        CohortOutcome.UNAVAILABLE,
        None,
        at,
        root_flow_id,
        tuple(sorted((key, POST_GRANT_INVARIANT) for key in keys)),
    )


class UnarmedPrecomputeRefusedError(InvariantError):
    """ADR-140 (B8): the Release found a skipped-finalizer shape it does not settle."""

    def __init__(self, run_id: str, grant_id: str, reason: str) -> None:
        super().__init__(
            UNARMED_PRECOMPUTE_REFUSED,
            "a winner's bake skipped the finalizer in a shape the Release does not settle.",
            context={"run_id": run_id, "grant_id": grant_id, "reason": reason},
        )
        self.reason = reason


def _is_fan_out(name: str) -> bool:
    return name.startswith(_FAN_OUT_PREFIX) or name == BAKE_FINALIZE_NODE


@dataclass(frozen=True, slots=True)
class UnarmedPrecomputeView:
    """The stored items the unarmed rule reads for one `GRANTED` winner (ADR-140).

    `nodes` is every `NODE#` of the run as `(name, state)`, PRE_GRANT included; `bake` and
    `precompute` are the grant's stored contributions; `at` is the Release's one clock read.
    """

    run_id: str
    grant_id: str
    root_flow_id: str
    nodes: tuple[tuple[str, NodeState], ...]
    bake: CohortContribution | None
    precompute: CohortContribution | None
    at: datetime

    def __post_init__(self) -> None:
        if not self.run_id or not self.grant_id or not self.root_flow_id:
            raise ValueError("an unarmed precompute view names its run, grant and root.")
        for member, contribution in (
            (CohortMember.BAKE, self.bake),
            (CohortMember.PRECOMPUTE, self.precompute),
        ):
            if contribution is not None and (
                contribution.grant_id != self.grant_id or contribution.member is not member
            ):
                raise ValueError("an unarmed precompute view's contribution is another member's.")
        if self.at.tzinfo is None or self.at.utcoffset() != timedelta(0):
            raise ValueError("the unarmed precompute clock must be timezone-aware UTC.")


def _unarmed_shape(bake: CohortContribution, state: NodeState) -> str | None:
    """Return the refusal reason for a stored bake member outside U1/U2, or None for U1/U2."""
    if bake.artifact_revision is None and (
        (bake.outcome is CohortOutcome.NO_ELIGIBLE and not bake.unavailable)
        or (
            bake.outcome is CohortOutcome.UNAVAILABLE
            and bake.unavailable
            and all(reason == POST_GRANT_INVARIANT for _key, reason in bake.unavailable)
        )
    ):
        expected = (
            NodeState.SUCCEEDED if bake.outcome is CohortOutcome.NO_ELIGIBLE else NodeState.FAILED
        )
        return None if state is expected else f"bake_node:{state.value}"
    return f"bake_shape:{bake.outcome.value}"


def _abandoned_shape(bake: CohortContribution, state: NodeState) -> str | None:
    """Return the refusal reason for an abandoned fan-out's bake member outside U3, or None.

    U3: the finalizer abandoned the cohort before precompute armed, so its bake member is the
    withheld one (`UNAVAILABLE`, no revision, every entry `post_grant_invariant`) and the
    fan-out node, which never contributes, `SUCCEEDED`.
    """
    if not (
        bake.artifact_revision is None
        and bake.outcome is CohortOutcome.UNAVAILABLE
        and bake.unavailable
        and all(reason == POST_GRANT_INVARIANT for _key, reason in bake.unavailable)
    ):
        return f"bake_shape:{bake.outcome.value}"
    return None if state is NodeState.SUCCEEDED else f"bake_node:{state.value}"


def unarmed_precompute_contribution(view: UnarmedPrecomputeView) -> CohortContribution | None:
    """ADR-140: the precompute member a bake that skipped the finalizer is owed, or None (wait).

    Only the finalizer arms precompute, so a bake that settled without one (U1 `NO_ELIGIBLE`, U2
    a consumer invariant) would leave precompute `WAITING_INPUT` forever. None while precompute
    is not `WAITING_INPUT`, the bake node is not terminal, no bake member is stored, or a
    fan-out's `bake_finalize` is not terminal. With no fan-out node and no stored precompute
    member, exactly U1 (`NO_ELIGIBLE`, bake `SUCCEEDED`) and U2 (every entry
    `post_grant_invariant`, bake `FAILED`), neither naming a revision, settle precompute
    `NO_ELIGIBLE`. With a fan-out whose finalizer abandoned the cohort (`bake_finalize`
    `FAILED`), exactly U3 (the withheld bake member, bake `SUCCEEDED`) settles the same way.
    Anything else is `UnarmedPrecomputeRefusedError`: a revision with no finalizer would bind a
    generation no pointer serves.
    """
    states = dict(view.nodes)
    for name in (CohortMember.PRECOMPUTE.value, CohortMember.BAKE.value):
        if name not in states:
            raise UnarmedPrecomputeRefusedError(view.run_id, view.grant_id, f"node_absent:{name}")
    bake_state = states[CohortMember.BAKE.value]
    if (
        states[CohortMember.PRECOMPUTE.value] is not NodeState.WAITING_INPUT
        or bake_state not in TERMINAL_NODE_STATES
        or view.bake is None
    ):
        return None
    reason: str | None
    finalize = states.get(BAKE_FINALIZE_NODE)
    if finalize is not None and finalize not in TERMINAL_NODE_STATES:
        return None  # the finalizer is still to settle (it abandons, or arms precompute)
    if any(_is_fan_out(name) for name in states):
        if finalize is not NodeState.FAILED:
            reason = "fan_out_present"
        elif view.precompute is not None:
            reason = "precompute_contributed"
        else:
            reason = _abandoned_shape(view.bake, bake_state)
    elif view.precompute is not None:
        reason = "precompute_contributed"
    else:
        reason = _unarmed_shape(view.bake, bake_state)
    if reason is not None:
        raise UnarmedPrecomputeRefusedError(view.run_id, view.grant_id, reason)
    return CohortContribution(
        view.grant_id,
        CohortMember.PRECOMPUTE,
        CohortOutcome.NO_ELIGIBLE,
        None,
        view.at,
        view.root_flow_id,
    )
