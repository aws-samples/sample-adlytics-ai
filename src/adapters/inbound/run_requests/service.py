# SPDX-License-Identifier: MIT-0
"""`admit_run_request`: the one application service every ingestion requester calls (S1).

Order of work, which is the order of the three refusal classes:

1. *Pre-domain* (no receipt, zero graph writes): schema, protocol version (BF-3), a
   `full_history` label (`FULL_HISTORY_NOT_ENABLED`), the caller/reason pairing (V-2), an agent
   principal (V-3), a legacy source (V-4), the key's `retrieved=` segment, and a manifest body
   that disagrees with its key (V-7c). A failed manifest read or listing is a recoverable
   dependency failure and writes no receipt either (V-7d).
2. *Domain decision*: the generated decision table over strongly read state. Exactly one receipt
   commits atomically with its effect (BF-10).
3. *Domain guards*: a transaction whose read-time conditions moved is re-read and re-decided,
   up to the generated bound. A guard never reaches the caller.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Final, Protocol

from adapters.jobs.execution_graph.instantiate import shadow_graph_for
from domain.operations.execution_graph import (
    AGENT_MUTATION_NOT_ENABLED,
    AuthorityMode,
    GraphCommandRefusedError,
    epoch_for_mode,
    graph_id,
    require_human_principal,
    require_no_legacy_source,
    require_protocol_v1,
    run_id,
)
from domain.operations.execution_graph_release import published_head
from domain.operations.generated_execution_graphs import (
    API_ONLY_REASONS,
    BOUNDS,
    CALLER_LABELS_REFUSED,
    CALLER_REASONS,
    CONTRACT_REVISION,
    DECISION_TABLE,
    HISTORICAL_POLICY,
    PROTOCOL_VERSION,
)
from domain.operations.ingestion_root import (
    SCHEMA_INVALID,
    ManifestVersion,
    RootRefusedError,
    manifest_fingerprint,
    require_body_agrees,
    root_flow_id,
    window_identity,
)
from domain.operations.run_request import (
    FULL_HISTORY_NOT_ENABLED,
    AdmissionClass,
    AdmissionGuardError,
    CallerKind,
    Decision,
    DecisionInputs,
    FingerprintMatch,
    HistoricalPolicy,
    ManifestFacts,
    MatchState,
    RetrievalEncoding,
    Waiter,
    decide,
    derive_admission_class,
    require_replaceable,
    require_sole_waiter,
)
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph import (
    AdmissionCommit,
    AdmissionConflictError,
    EffectKind,
    ExecutionGraphStore,
    ManifestReader,
    NewRun,
    ReceiptRecord,
    ReplacedRun,
    RunRequestNotRecordedError,
    UnitState,
    conflict_receipt_sk,
)

__all__ = [
    "INGESTION_TEMPLATE",
    "RUN_REQUEST_REASON_NOT_PERMITTED",
    "AdmissionDependencies",
    "LegacyLookupUnavailableError",
    "LegacyPlans",
    "RunRequest",
    "admit_run_request",
    "contract_policy",
    "request_digest",
    "require_workload_requester",
]

INGESTION_TEMPLATE: Final = "ingestion_manifest_to_served.v1"
RUN_REQUEST_REASON_NOT_PERMITTED: Final = "RUN_REQUEST_REASON_NOT_PERMITTED"
CONFLICT_UNIT_HISTORY_OVER_BOUND: Final = "conflict_unit_history_over_bound"
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_CONFLICT_ROW: Final = 2


class LegacyLookupUnavailableError(DependencyError):
    """Epoch 1 needs the legacy-completed read (row 9), which this deployment does not carry."""

    def __init__(self) -> None:
        super().__init__(ErrorCode("RUN_REQUEST", 13), "the legacy plan lookup is not wired.")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """`RunRequest.v1` plus the authenticated requester the boundary resolved."""

    request_id: str
    protocol_version: str
    caller_kind: str
    reason: str
    manifest_key: str
    version_id: str
    requester: str
    """The authenticated principal: a Cognito subject digest or an IAM role/user ARN."""
    requester_is_iam: bool
    via_api_group: bool = False
    """True only for an on-demand API caller the boundary verified in the requester group."""
    label: str | None = None
    workload: bool = False
    """S6 (S1A-7): set only by a deployed ingestion handler's own code, never from a request
    body. A workload role may submit only its own automatic caller kind (`delivery_event` from
    the SQS handler, `schedule` from the registration finalizer); every other IAM requester is
    held to the human-principal rule."""


class LegacyPlans(Protocol):
    """Legacy authority in epoch 1: row 9's completed plan and `H(U)`'s legacy head (S4)."""

    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        """Return the completed legacy plan's key, or None."""
        ...

    def head_after(self, window: str, r: datetime) -> datetime | None:
        """Return the newest retrieval of the window above `r` whose legacy plan COMPLETED."""
        ...


@dataclass(frozen=True, slots=True)
class AdmissionDependencies:
    """The store, the manifest reader and the clock admission runs against."""

    store: ExecutionGraphStore
    manifests: ManifestReader
    now: Callable[[], datetime]
    legacy: LegacyPlans | None = None


def contract_policy() -> HistoricalPolicy:
    """Return the replay band pinned in the generated contract (equal to the planner's)."""
    pinned = HISTORICAL_POLICY
    return HistoricalPolicy(
        replay_base=datetime.fromisoformat(str(pinned["replay_base"]).replace("Z", "+00:00")),
        first_day=date.fromisoformat(str(pinned["first_day"])),
        cut=date.fromisoformat(str(pinned["cut"])),
        stride_seconds=int(pinned["stride_seconds"]),
        seal_slot_base=int(pinned["seal_slot_base"]),
        backfill_slot_base=int(pinned["backfill_slot_base"]),
        probe_slot_base=int(pinned["probe_slot_base"]),
        probe_slot_count=int(pinned["probe_slot_count"]),
        encodings={
            (str(item["source_system"]), str(item["source_dataset"])): RetrievalEncoding(
                str(item["encoding"])
            )
            for item in pinned["encodings"]
        },
    )


def request_digest(request: RunRequest) -> str:
    """Return the digest a duplicate request ID is compared by (rows 1 and 2)."""
    return canonical_authority_digest(
        [
            "run-request/v1",
            request.request_id,
            request.protocol_version,
            request.caller_kind,
            request.reason,
            request.manifest_key,
            request.version_id,
            request.label or "",
        ]
    )


_WORKLOAD_ROLE: Final = re.compile(
    r"^arn:aws:sts::\d{12}:assumed-role/(?!AWSReservedSSO_)[\w+=,.@-]+/[\w+=,.@-]+$"
)
_WORKLOAD_CALLERS: Final = frozenset({CallerKind.DELIVERY_EVENT, CallerKind.SCHEDULE})


def require_workload_requester(requester: str, caller: CallerKind) -> None:
    """S1A-7: a workload role submits only an automatic caller kind, under its own role session.

    A human session (SSO or IAM user) is never a workload; an `on_demand` request from a role
    is held to the human-principal rule and refused as an agent mutation.
    """
    if caller not in _WORKLOAD_CALLERS or _WORKLOAD_ROLE.fullmatch(requester) is None:
        raise GraphCommandRefusedError(
            AGENT_MUTATION_NOT_ENABLED,
            "only an ingestion workload role may submit an automatic request as itself.",
        )


def _pre_domain(request: RunRequest) -> CallerKind:
    """Every refusal that writes no receipt, before any read."""
    if (
        _REQUEST_ID.fullmatch(request.request_id) is None
        or not request.manifest_key.strip()
        or not request.version_id.strip()
        or not request.requester.strip()
    ):
        raise RootRefusedError(SCHEMA_INVALID, "a run request field is missing or malformed.")
    require_protocol_v1(request.protocol_version)
    if request.label in CALLER_LABELS_REFUSED:
        raise GraphCommandRefusedError(
            FULL_HISTORY_NOT_ENABLED, "full-history requests are not enabled."
        )
    try:
        caller = CallerKind(request.caller_kind)
    except ValueError:
        raise RootRefusedError(
            SCHEMA_INVALID, "the caller kind is not in the closed set."
        ) from None
    permitted = CALLER_REASONS[caller.value]
    if request.reason not in permitted or (
        request.reason in API_ONLY_REASONS and not request.via_api_group
    ):
        raise GraphCommandRefusedError(
            RUN_REQUEST_REASON_NOT_PERMITTED, "this caller may not request with this reason."
        )
    if request.requester_is_iam and request.workload:
        require_workload_requester(request.requester, caller)
    elif request.requester_is_iam:
        require_human_principal(request.requester)
    require_no_legacy_source(request.manifest_key, request.version_id)
    return caller


@dataclass(frozen=True, slots=True)
class _Read:
    """Everything one decision reads."""

    request: RunRequest
    inputs: DecisionInputs
    mode: AuthorityMode
    unit: UnitState | None
    over_bound: bool
    root_clock: str | None = None
    """S5: the manifest version's `LastModified`, when admission read the manifest (FR-2)."""


def _read(
    request: RunRequest, caller: CallerKind, deps: AdmissionDependencies
) -> tuple[_Read, ReceiptRecord | None]:
    identity = window_identity(request.manifest_key)
    version = ManifestVersion(request.manifest_key, request.version_id)
    # `F` keys on the contract revision, never on the contract file's bytes: an edit that is
    # not a reviewed revision must not re-key every input (design.md, "Input fingerprint").
    fingerprint = manifest_fingerprint(version, INGESTION_TEMPLATE, int(CONTRACT_REVISION))
    store = deps.store
    existing = store.read_receipt(request.request_id)
    mode_state = store.read_mode()
    mode = mode_state.mode
    epoch = epoch_for_mode(mode)
    # V-14 (S3): the hold lives on the mode item; it can be set only in GRAPH (r5, R4-10).
    hold = mode_state.hold and mode is AuthorityMode.GRAPH
    admitting = mode in {AuthorityMode.SHADOW, AuthorityMode.GRAPH}
    cls, versions, over_bound = AdmissionClass.CADENCE, 1, False
    unit: UnitState | None = None
    match: FingerprintMatch | None = None
    root_clock: str | None = None
    if existing is None and admitting:
        manifest = deps.manifests.read(request.manifest_key, request.version_id)
        if manifest.last_modified is None:
            # S5-V-5: the root clock is required on the admission path; no receipt is written.
            raise RunRequestNotRecordedError("the manifest version carries no root clock.")
        root_clock = manifest.last_modified.astimezone(UTC).isoformat()
        require_body_agrees(identity, manifest.retrieved_at)
        cls = derive_admission_class(
            ManifestFacts(
                manifest.source_system,
                manifest.source_dataset,
                date.fromisoformat(manifest.period_end),
                manifest.retrieved_at,
            ),
            contract_policy(),
            request.reason,
        )
        versions = manifest.exact_versions
        over_bound = manifest.window_retrievals > int(BOUNDS["max_window_retrievals"])
        match = store.read_match(fingerprint, epoch)
        if epoch == 1:
            unit = store.read_unit(identity.conflict_unit, epoch)
            if deps.legacy is None:
                raise LegacyLookupUnavailableError
            if match is None:
                plan = deps.legacy.completed_plan_key(request.manifest_key, request.version_id)
                if plan is not None:
                    match = FingerprintMatch(MatchState.LEGACY_COMPLETED, legacy_plan_key=plan)
            # S4 (BF-8): row 13 compares `r` with the same `H(U)` release uses, the larger
            # of the graph head and the legacy head of newer retrievals of this window.
            legacy_head = deps.legacy.head_after(identity.window, identity.retrieved_at)
            unit = replace(unit, head=published_head(unit.head, legacy_head))
    inputs = DecisionInputs(
        request_digest=request_digest(request),
        receipt_digest=None if existing is None else existing.request_digest,
        epoch=epoch,
        admitting=admitting,
        hold=hold,
        caller=caller,
        reason=request.reason,
        admission_class=cls,
        version_count=versions,
        blocked=False if unit is None else unit.blocked,
        fingerprint=fingerprint,
        r=identity.retrieved_at,
        active=None if unit is None else unit.active,
        waiter=None if unit is None else unit.waiter,
        match=match,
        head=None if unit is None else unit.head,
    )
    return _Read(request, inputs, mode, unit, over_bound, root_clock), existing


def _receipt(
    read: _Read, decision: Decision, recorded_at: datetime, **ids: str | None
) -> ReceiptRecord:
    request = read.request
    identity = window_identity(request.manifest_key)
    match = read.inputs.match
    retried = None if decision.retry_generation is None or match is None else match.run_id
    return ReceiptRecord(
        request_id=request.request_id,
        request_digest=read.inputs.request_digest,
        decision=decision.decision,
        reason=decision.reason,
        row=decision.row or None,
        authority_epoch=read.inputs.epoch,
        mode=read.mode,
        conflict_unit=identity.conflict_unit,
        input_revision=identity.retrieved_at.isoformat(),
        fingerprint=read.inputs.fingerprint,
        recorded_at=recorded_at.astimezone(UTC).isoformat(),
        protocol_version=PROTOCOL_VERSION,
        contract_revision=int(CONTRACT_REVISION),
        root_flow_id=root_flow_id(ManifestVersion(request.manifest_key, request.version_id)),
        retry_generation=decision.retry_generation,
        caller_kind=request.caller_kind,
        caller_reason=request.reason,
        requester=canonical_authority_digest(["requester/v1", request.requester]),
        retried_run_id=retried,
        root_clock_started_at=read.root_clock,
        # S6 §2.4: an epoch-1 (GRAPH) receipt names the admitted manifest version, which the
        # run's `materialize_raw` node reads as its inputs. SHADOW receipts are unchanged.
        manifest_key=request.manifest_key if read.inputs.epoch == 1 else None,
        version_id=request.version_id if read.inputs.epoch == 1 else None,
        **ids,  # pyright: ignore[reportArgumentType]
    )


def _commit(read: _Read, decision: Decision, now: datetime) -> AdmissionCommit:
    """Map one decision to its receipt and conditioned effect."""
    request = read.request
    inputs = read.inputs
    identity = window_identity(request.manifest_key)
    unit = read.unit
    slot_revision = 0 if unit is None else unit.slot_revision
    generation = decision.retry_generation or 0
    root = root_flow_id(ManifestVersion(request.manifest_key, request.version_id))
    if decision.decision in {"REFUSED", "JOINED"}:
        joined: dict[str, str | None] = {}
        if decision.decision == "JOINED":
            match = inputs.match
            if decision.row == 7 and inputs.active is not None:  # noqa: PLR2004
                joined["run_id"] = inputs.active.run_id
            elif decision.row == 8 and inputs.waiter is not None:  # noqa: PLR2004
                joined["joined_request_id"] = inputs.waiter.request_id
            elif match is not None and match.state is MatchState.SHADOW_RECORDED:
                joined["graph_id"] = match.run_id
            elif match is not None:
                joined["run_id"] = match.run_id
                joined["legacy_plan_key"] = match.legacy_plan_key
        receipt = _receipt(read, decision, now, **joined)
        # Row 2 refuses against the receipt that already holds `RECEIPT`, so its own receipt
        # takes the conflicting digest's key; it can always commit exactly once (BF-10).
        return AdmissionCommit(
            receipt,
            EffectKind.NONE,
            identity.conflict_unit,
            slot_revision,
            receipt_sk=(
                conflict_receipt_sk(inputs.request_digest)
                if decision.row == _CONFLICT_ROW
                else "RECEIPT"
            ),
        )
    if decision.decision == "ADMITTED" and inputs.epoch == 0:
        name = graph_id(inputs.fingerprint, 0, generation)
        receipt = _receipt(read, decision, now, graph_id=name)
        return AdmissionCommit(
            receipt,
            EffectKind.SHADOW_GRAPH,
            identity.conflict_unit,
            slot_revision,
            shadow_graph=shadow_graph_for(receipt, INGESTION_TEMPLATE),
        )
    new_run = NewRun(
        run_id=run_id(inputs.fingerprint, inputs.epoch, generation),
        graph_id=graph_id(inputs.fingerprint, inputs.epoch, generation),
        fingerprint=inputs.fingerprint,
        retrieved_at=identity.retrieved_at.isoformat(),
        root_flow_id=root,
        generation=generation,
        template=INGESTION_TEMPLATE,
        window=identity.window,
        root_clock_started_at=read.root_clock,
    )
    superseded = inputs.waiter
    if decision.decision == "QUEUED_LATEST":
        require_sole_waiter(inputs.waiter, supersedes=True)
        waiter = Waiter(
            request.request_id,
            inputs.fingerprint,
            identity.retrieved_at,
            held=decision.row == 16,  # noqa: PLR2004
            revision=1 if superseded is None else superseded.revision + 1,
            window=identity.window,
            root_clock_started_at=read.root_clock,
        )
        receipt = _receipt(read, decision, now)
        return AdmissionCommit(
            receipt,
            EffectKind.QUEUE_WAITER,
            identity.conflict_unit,
            slot_revision,
            new_waiter=waiter,
            superseded_waiter=superseded,
            wake_held=decision.row == 16,  # noqa: PLR2004
        )
    receipt = _receipt(read, decision, now, run_id=new_run.run_id, graph_id=new_run.graph_id)
    if decision.decision in {"REPLACED_BEFORE_COMPUTE", "REPLACED_ON_INVALIDATION"}:
        active = inputs.active
        if active is None:
            raise RunRequestNotRecordedError("a replacing decision read no active run.")
        invalidating = decision.decision == "REPLACED_ON_INVALIDATION"
        require_replaceable(active, invalidating=invalidating)
        return AdmissionCommit(
            receipt,
            EffectKind.REPLACE_RUN,
            identity.conflict_unit,
            slot_revision,
            new_run=new_run,
            replaced=ReplacedRun(active.run_id, invalidating, active.root_clock_started_at),
            superseded_waiter=superseded,
        )
    return AdmissionCommit(
        receipt,
        EffectKind.ADMIT_RUN,
        identity.conflict_unit,
        slot_revision,
        new_run=new_run,
        superseded_waiter=superseded,
    )


def admit_run_request(request: RunRequest, deps: AdmissionDependencies) -> ReceiptRecord:
    """Decide one request and commit exactly one receipt with its effect (or return the old one)."""
    caller = _pre_domain(request)
    for _ in range(int(BOUNDS["max_redecide"])):
        read, existing = _read(request, caller, deps)
        if read.over_bound:
            decision = Decision(0, "REFUSED", CONFLICT_UNIT_HISTORY_OVER_BOUND)
        else:
            decision = decide(DECISION_TABLE, read.inputs)
        if decision.decision == "RETURN_RECEIPT" and existing is not None:
            return existing
        if decision.row == _CONFLICT_ROW:
            # An exact replay of a conflicting request returns its own row-2 receipt.
            prior = deps.store.read_conflict_receipt(request.request_id, read.inputs.request_digest)
            if prior is not None:
                return prior
        try:
            commit = _commit(read, decision, deps.now())
            deps.store.commit_admission(commit)
        except AdmissionConflictError, AdmissionGuardError:
            continue
        return commit.receipt
    raise RunRequestNotRecordedError("the decision could not commit within the re-decide bound.")
