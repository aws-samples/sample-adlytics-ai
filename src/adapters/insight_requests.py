# SPDX-License-Identifier: MIT-0
"""Contract-driven construction of immutable model-insight assembly requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.insight_next_steps import (
    NextStepSurface,
    available_next_steps_entry,
    next_step_options,
)
from domain.presentation.insights import (
    INSIGHT_MODEL_SILENCE_REASON,
    DeterministicFinding,
    InsightCacheKey,
    InsightContextEntry,
    InsightContract,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightOutputClass,
    InsightSlotId,
    InsightWindowProfile,
    PredicateQuietReason,
    evaluate_slot_predicate,
    insight_input_digest,
    insight_input_document,
    insight_quiet_reason,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.values import DateRange

_ACTIVE_PROMPT_VERSION = "active-pointer"
_ANALYTICAL_SCOPE_ID_VERSION = "analytical-scope-v1"


def analytical_scope_id(scope: AuthorizedAnalyticalScope) -> str:
    """Return the versioned canonical deployment-scope cache identity."""
    return f"{_ANALYTICAL_SCOPE_ID_VERSION}:" + ",".join(
        sorted(item.value for item in scope.commerce_scopes)
    )


@dataclass(frozen=True, slots=True)
class InsightRequestContext:
    """Server-derived cache identity shared by interactive and precompute assembly."""

    analytical_scope_id: str
    subject: str
    window: DateRange
    artifact_revision: str
    filter_id: str = "all"
    """The channel filter sibling these slots read (ADR-127); part of every cache key."""

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.analytical_scope_id, self.subject, self.artifact_revision)
        ):
            raise ValueError(
                "insight request context must contain server-derived nonblank identity."
            )


@dataclass(frozen=True, slots=True)
class DeclaredInsightRequestInput:
    """All contract and fact inputs necessary to create one declared request batch."""

    context: InsightRequestContext
    contracts: Mapping[InsightSlotId, InsightContract]
    findings_by_slot: Mapping[InsightSlotId, tuple[DeterministicFinding, ...]]
    slot_ids: tuple[InsightSlotId, ...]
    window_profile: InsightWindowProfile
    evidence_by_slot: Mapping[InsightSlotId, InsightEvidenceBundle] | None = None
    display_context_by_slot: Mapping[InsightSlotId, tuple[InsightContextEntry, ...]] | None = None
    next_step_surface_by_slot: Mapping[InsightSlotId, NextStepSurface] | None = None
    """FR-11: what each card's page serves; read only when its contract allows a kind."""


@dataclass(frozen=True, slots=True)
class _CardModelInput:
    """The five fields the card's model input is built from, digested into its key."""

    purpose: str
    output_class: InsightOutputClass
    window_profile: InsightWindowProfile
    context: tuple[InsightContextEntry, ...]
    findings: tuple[DeterministicFinding, ...]


@dataclass(frozen=True, slots=True)
class DeclaredInsightRequestBatch:
    """Every declared slot's classified outcome: admitted, evaluated-quiet, or ineligible.

    Absence of a request is never the signal (declared-outcomes rule): each slot the
    batch does not admit appears in exactly one classification map, so the serve path
    can name the business reason instead of a generic fallback.
    """

    requests: tuple[InsightAssemblyRequest, ...]
    silence_reasons: Mapping[InsightSlotId, str]
    missing_evidence_by_slot: Mapping[InsightSlotId, tuple[InsightEvidenceInput, ...]]


def declared_insight_requests(
    request: DeclaredInsightRequestInput,
) -> DeclaredInsightRequestBatch:
    """Classify every declared model slot from server-computed findings and context."""
    requests: list[InsightAssemblyRequest] = []
    silence_reasons: dict[InsightSlotId, str] = {}
    missing_evidence_by_slot: dict[InsightSlotId, tuple[InsightEvidenceInput, ...]] = {}
    for slot_id in request.slot_ids:
        contract = request.contracts.get(slot_id)
        if contract is None or contract.id is InsightSlotId.MOVERS:
            raise ValueError("insight request must name a declared model-authored slot.")
        evaluation = evaluate_slot_predicate(
            contract,
            (request.evidence_by_slot or {}).get(slot_id),
            request.window_profile.role,
        )
        if evaluation.missing_evidence:
            missing_evidence_by_slot[slot_id] = evaluation.missing_evidence
            continue
        if not evaluation.findings:
            # The served customer reason names why Gate-1 went quiet (ADR-068 2026-10-05);
            # the contract's silence condition stays its governance statement.
            silence_reasons[slot_id] = insight_quiet_reason(
                slot_id, cast("PredicateQuietReason", evaluation.quiet_reason)
            )
            continue
        context = request.context
        options = next_step_options(
            (request.next_step_surface_by_slot or {}).get(slot_id), contract.next_step_kinds
        )
        options_entry = available_next_steps_entry(options)
        model_input = _CardModelInput(
            purpose=contract.purpose,
            output_class=contract.output_class,
            window_profile=request.window_profile,
            context=(
                *(request.display_context_by_slot or {}).get(slot_id, ()),
                *(() if options_entry is None else (options_entry,)),
            ),
            findings=evaluation.findings,
        )
        requests.append(
            InsightAssemblyRequest(
                key=InsightCacheKey(
                    analytical_scope_id=context.analytical_scope_id,
                    subject=context.subject,
                    window_start=context.window.start.isoformat(),
                    window_end=context.window.end.isoformat(),
                    artifact_revision=context.artifact_revision,
                    prompt_version=_ACTIVE_PROMPT_VERSION,
                    slot_id=slot_id,
                    # ADR-076 decision 1 (2026-10-05): the card key carries its input.
                    input_digest=insight_input_digest(insight_input_document(model_input)),
                    filter_id=context.filter_id,
                ),
                output_class=model_input.output_class,
                purpose=model_input.purpose,
                findings=model_input.findings,
                silence_reason=INSIGHT_MODEL_SILENCE_REASON,
                window_profile=model_input.window_profile,
                context=model_input.context,
                next_step_options=options,
            )
        )
    return DeclaredInsightRequestBatch(
        requests=tuple(requests),
        silence_reasons=silence_reasons,
        missing_evidence_by_slot=missing_evidence_by_slot,
    )
