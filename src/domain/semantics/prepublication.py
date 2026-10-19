# SPDX-License-Identifier: MIT-0
"""Pure pre-conformance, reconciliation, suppression, and publication gate decisions.

The gate consumes evidence; it performs no I/O and cannot make bytes current. Adapters evaluate
candidate tables and source deliveries, compile their outcomes into these types, and only a complete
publishable decision may reach the fenced publication transition in ``publication.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import PartitionIdentity, PublicationLensKey, SourcePublicationRef
from domain.semantics.revision import SourceRevision
from domain.semantics.source_pack import (
    PackReadiness,
    SourcePack,
    SourcePackBlockerFamily,
)
from domain.semantics.time import FactTimeClass, MeasurementBucket
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.provenance import MeasurementBasis

__all__ = [
    "CandidateMeasureTotal",
    "ControlComparisonKey",
    "ControlComparisonScope",
    "ControlTotalRequest",
    "PrepublicationDecision",
    "PrepublicationRequest",
    "ReportedMeasureTotal",
    "SourcePackConformanceRequest",
    "SourcePackConformanceResult",
    "SuppressionCoverageEvidence",
    "WithheldResidualTotal",
    "conform_source_pack",
    "evaluate_control_total",
    "evaluate_prepublication",
    "evaluate_suppression_coverage",
]

_SOURCE_FAMILIES = frozenset(
    {
        PrepublicationRuleFamily.PROVENANCE,
        PrepublicationRuleFamily.IDENTITY,
        PrepublicationRuleFamily.TIME,
    }
)


@dataclass(frozen=True, slots=True)
class SourcePackConformanceRequest:
    """One source publication and all pre-conformance checks for its declared pack."""

    pack: SourcePack
    source_publication: SourcePublicationRef
    results: tuple[PrepublicationRuleResult, ...]

    def __post_init__(self) -> None:
        families = frozenset(result.family for result in self.results)
        if families != _SOURCE_FAMILIES:
            missing = _SOURCE_FAMILIES - families
            extra = families - _SOURCE_FAMILIES
            raise ValueError(
                "source-pack conformance requires provenance, identity, and time only; "
                f"missing={sorted(item.value for item in missing)}, "
                f"extra={sorted(item.value for item in extra)}."
            )
        if len(self.results) != len(_SOURCE_FAMILIES):
            raise ValueError("source-pack conformance requires one result per source family.")


@dataclass(frozen=True, slots=True)
class SourcePackConformanceResult:
    """Typed evidence for one exact source-aligned publication under one source pack."""

    pack: SourcePack
    source_publication: SourcePublicationRef
    results: tuple[PrepublicationRuleResult, ...]

    def __post_init__(self) -> None:
        families = frozenset(result.family for result in self.results)
        if families != _SOURCE_FAMILIES or len(self.results) != len(_SOURCE_FAMILIES):
            raise ValueError("source-pack result requires provenance, identity, and time once.")
        blocker = self.pack.blocker
        if self.pack.readiness is PackReadiness.BLOCKED:
            if blocker is None:  # pragma: no cover - SourcePack itself rejects this
                raise ValueError("blocked source pack has no blocker.")
            blocked_family = {
                SourcePackBlockerFamily.IDENTITY: PrepublicationRuleFamily.IDENTITY,
                SourcePackBlockerFamily.TIME: PrepublicationRuleFamily.TIME,
            }[blocker.family]
            outcome = next(
                result.outcome for result in self.results if result.family is blocked_family
            )
            if outcome is not RuleOutcome.FAIL:
                raise ValueError(
                    f"blocked source pack must fail its {blocked_family.value} evidence."
                )

    @property
    def contract_id(self) -> str:
        """Contract declaration this conformance evidence evaluates."""
        return self.pack.contract_id

    @property
    def publishable(self) -> bool:
        """Whether this source input passed identity, time, and provenance conformance."""
        return all(result.outcome is not RuleOutcome.FAIL for result in self.results)


def conform_source_pack(request: SourcePackConformanceRequest) -> SourcePackConformanceResult:
    """Fail a blocked pack in its typed evidence family, regardless of caller optimism."""
    results = request.results
    blocker = request.pack.blocker
    if request.pack.readiness is PackReadiness.BLOCKED:
        if blocker is None:  # pragma: no cover - SourcePack itself rejects this
            raise ValueError("blocked source pack has no blocker.")
        blocked_family = {
            SourcePackBlockerFamily.IDENTITY: PrepublicationRuleFamily.IDENTITY,
            SourcePackBlockerFamily.TIME: PrepublicationRuleFamily.TIME,
        }[blocker.family]
        results = tuple(
            replace(
                result,
                outcome=RuleOutcome.FAIL,
                detail=blocker.detail,
                qualifier=None,
            )
            if result.family is blocked_family
            else result
            for result in results
        )
    return SourcePackConformanceResult(
        pack=request.pack,
        source_publication=request.source_publication,
        results=results,
    )


@dataclass(frozen=True, slots=True)
class ControlComparisonScope:
    """Dataset-independent semantics shared by one detail/reported control pair."""

    group: str
    commerce_scope: CommerceScopeKey
    population_key: str
    lens: PublicationLensKey
    time_class: FactTimeClass
    bucket: MeasurementBucket
    delivery_identity: str

    def __post_init__(self) -> None:
        if not self.group.strip() or not self.population_key.strip():
            raise ValueError("control comparison requires group and population identities.")
        if not self.delivery_identity.strip():
            raise ValueError("control comparison requires exact landed-delivery identity.")


@dataclass(frozen=True, slots=True)
class ControlComparisonKey:
    """Exact semantics under which detail and reported totals may reconcile."""

    scope: ControlComparisonScope
    source_revision: SourceRevision
    measurement_basis: MeasurementBasis
    unit: str

    def __post_init__(self) -> None:
        if not self.unit.strip():
            raise ValueError("control-total comparison requires a unit.")


@dataclass(frozen=True, slots=True)
class WithheldResidualTotal:
    """Withheld quantity carrying its own exact reconciliation semantics."""

    key: ControlComparisonKey
    value: Decimal

    def __post_init__(self) -> None:
        if not self.value.is_finite():
            raise ValueError("withheld residual quantity must be finite.")


@dataclass(frozen=True, slots=True)
class CandidateMeasureTotal:
    """Attributable candidate quantity and separately keyed withheld evidence."""

    measure: str
    key: ControlComparisonKey
    attributable: Decimal | None
    withheld_residual: WithheldResidualTotal | None
    suppression_observed: bool

    def __post_init__(self) -> None:
        if not self.measure.strip():
            raise ValueError("candidate control total requires a measure name.")
        if self.attributable is not None and not self.attributable.is_finite():
            raise ValueError("candidate control-total quantities must be finite.")


@dataclass(frozen=True, slots=True)
class ReportedMeasureTotal:
    """Source-reported control quantity retained as evidence, not canonical detail."""

    measure: str
    key: ControlComparisonKey
    value: Decimal | None

    def __post_init__(self) -> None:
        if not self.measure.strip():
            raise ValueError("reported control total requires a measure name.")
        if self.value is not None and not self.value.is_finite():
            raise ValueError("reported control-total quantity must be finite.")


@dataclass(frozen=True, slots=True)
class ControlTotalRequest:
    """One candidate measure and the exact reported total it claims to reconcile with."""

    candidate: CandidateMeasureTotal
    reported: ReportedMeasureTotal


def evaluate_control_total(request: ControlTotalRequest) -> PrepublicationRuleResult:
    """Compare attributable plus residual only under an identical semantic key."""
    candidate = request.candidate
    reported = request.reported
    outcome = RuleOutcome.FAIL
    if candidate.measure != reported.measure:
        detail = "Candidate and reported control evidence name different measures."
    elif candidate.key != reported.key:
        detail = (
            "Candidate and reported control evidence differ in exact bucket, scope/options, "
            "revision, basis, or unit."
        )
    elif candidate.attributable is None and candidate.withheld_residual is None:
        if reported.value is None and not candidate.suppression_observed:
            outcome = RuleOutcome.PASSED
            detail = "Candidate and reported control both declare the measure unavailable."
        else:
            detail = "Candidate and reported control disagree on measure availability."
    elif reported.value is None:
        detail = "Candidate and reported control disagree on measure availability."
    elif candidate.suppression_observed and candidate.withheld_residual is None:
        detail = "Suppression was observed but matching withheld residual evidence is absent."
    elif (
        candidate.withheld_residual is not None and candidate.withheld_residual.key != candidate.key
    ):
        detail = (
            "Withheld residual evidence differs from candidate exact bucket, scope/options, "
            "revision, basis, or unit."
        )
    else:
        residual = candidate.withheld_residual
        residual_value = Decimal(0) if residual is None else residual.value
        attributable = Decimal(0) if candidate.attributable is None else candidate.attributable
        candidate_total = attributable + residual_value
        if candidate_total == reported.value:
            outcome = RuleOutcome.PASSED
            detail = "Attributable plus matching withheld residual equals the exact reported total."
        else:
            detail = (
                f"Attributable plus withheld residual is {candidate_total}; source reported "
                f"{reported.value}."
            )
    return PrepublicationRuleResult(
        family=PrepublicationRuleFamily.CONTROL_TOTALS,
        rule_id=f"control-total:{candidate.measure}",
        outcome=outcome,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class SuppressionCoverageEvidence:
    """Exact requested buckets and the subset suppression prevents assigning safely."""

    requested_buckets: tuple[MeasurementBucket, ...]
    unavailable_buckets: tuple[MeasurementBucket, ...] = ()

    def __post_init__(self) -> None:
        if not self.requested_buckets:
            raise ValueError("suppression coverage requires requested measurement buckets.")
        if len(set(self.requested_buckets)) != len(self.requested_buckets):
            raise ValueError("suppression coverage repeats a requested bucket.")
        if len(set(self.unavailable_buckets)) != len(self.unavailable_buckets):
            raise ValueError("suppression coverage repeats an unavailable bucket.")
        unknown = set(self.unavailable_buckets) - set(self.requested_buckets)
        if unknown:
            raise ValueError("suppression coverage marks an unrequested bucket unavailable.")

    @property
    def proven_buckets(self) -> tuple[MeasurementBucket, ...]:
        """Buckets safe to publish; no extraction envelope is inferred."""
        unavailable = set(self.unavailable_buckets)
        return tuple(bucket for bucket in self.requested_buckets if bucket not in unavailable)


def evaluate_suppression_coverage(
    partition: PartitionIdentity,
    evidence: SuppressionCoverageEvidence,
) -> PrepublicationRuleResult:
    """Permit only a candidate bucket proved independently of suppression holes."""
    if partition.bucket not in evidence.requested_buckets:
        return PrepublicationRuleResult(
            family=PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
            rule_id="suppression:exact-bucket",
            outcome=RuleOutcome.FAIL,
            detail="Candidate bucket is absent from exact requested suppression evidence.",
        )
    if partition.bucket in evidence.unavailable_buckets:
        return PrepublicationRuleResult(
            family=PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
            rule_id="suppression:exact-bucket",
            outcome=RuleOutcome.FAIL,
            detail="Suppression prevents assigning the candidate bucket exactly.",
        )
    return PrepublicationRuleResult(
        family=PrepublicationRuleFamily.SUPPRESSION_COVERAGE,
        rule_id="suppression:exact-bucket",
        outcome=RuleOutcome.PASSED,
        detail="Candidate bucket is explicitly proven outside every suppression hole.",
    )


@dataclass(frozen=True, slots=True)
class PrepublicationRequest:
    """All source conformance and candidate checks needed for one gate decision."""

    rule_set_version: str
    subject: PublicationEvidenceSubject
    source_conformance: tuple[SourcePackConformanceResult, ...]
    candidate_results: tuple[PrepublicationRuleResult, ...]

    def __post_init__(self) -> None:
        if not self.source_conformance:
            raise ValueError("prepublication gate requires source-pack conformance evidence.")
        source_refs = tuple(item.source_publication for item in self.source_conformance)
        if len(set(source_refs)) != len(source_refs):
            raise ValueError("prepublication gate repeats source publication evidence.")
        if tuple(reference.value for reference in source_refs) != self.subject.source_publications:
            raise ValueError("prepublication subject differs from source conformance publications.")
        if any(result.family in _SOURCE_FAMILIES for result in self.candidate_results):
            raise ValueError("candidate checks cannot replace source-pack conformance families.")


@dataclass(frozen=True, slots=True)
class PrepublicationDecision:
    """Complete immutable evidence and the single publishability decision derived from it."""

    evidence: PublicationEvidence

    @property
    def publishable(self) -> bool:
        """Whether the fenced publication transition may be attempted."""
        return self.evidence.publishable


def evaluate_prepublication(request: PrepublicationRequest) -> PrepublicationDecision:
    """Require all closed families and preserve every source-specific result."""
    source_results = tuple(
        result for conformance in request.source_conformance for result in conformance.results
    )
    return PrepublicationDecision(
        PublicationEvidence(
            rule_set_version=request.rule_set_version,
            subject=request.subject,
            results=(*source_results, *request.candidate_results),
        )
    )
