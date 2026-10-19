# SPDX-License-Identifier: MIT-0
"""Reusable Semantic V2 publication values and selection stub for unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from domain.catalog.freshness import Coverage
from domain.measurement.planning import QueryPlan
from domain.measurement.publications import BoundQueryPlan
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import (
    AttemptId,
    CanonicalDatasetKey,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationId,
    PublicationLensKey,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, MeasurementBucket, TimeGrain
from domain.semantics.validation import LegacyPublicationRules, PublicationEvidence
from domain.shared_kernel.provenance import FreshnessStatus
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
    PresentationEvidenceInspectionSlot,
    PublicationSelection,
    PublicationSelectionRequest,
)

_CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
_PUBLISHER = CanonicalPublisherKey("unit-test-publisher")


@dataclass(frozen=True, slots=True)
class _PublicationSpec:
    publication_id: str
    published_at: datetime
    qualified_dq: tuple[str, ...] = ()


def successful_publication(
    dataset: str,
    day: date,
    spec: _PublicationSpec,
) -> SuccessfulPublication:
    """Build a valid readable historical success for one daily canonical partition."""
    partition = PartitionIdentity(
        scope=PublicationScope(
            dataset=CanonicalDatasetKey(dataset),
            commerce_scope=CommerceScopeKey("amazon_us"),
            population_key="unit_test_population",
            lens=PublicationLensKey("unit_test_lens"),
            time_class=FactTimeClass.DAILY_ADDITIVE,
            business_calendar=_CALENDAR,
            source_timezone="UTC",
        ),
        bucket=MeasurementBucket(
            window=DateRange(day, day),
            grain=TimeGrain.DAY,
            calendar=_CALENDAR,
            source_timezone="UTC",
        ),
    )
    source_id = f"source-{spec.publication_id}"
    return SuccessfulPublication(
        publication_id=PublicationId(spec.publication_id),
        publisher=_PUBLISHER,
        partition=partition,
        attempt_id=AttemptId(f"attempt-{spec.publication_id}"),
        source_inputs=(SourcePublicationRef(source_id),),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("unit.test_revision"),
            source_id=source_id,
            observed_at=spec.published_at,
        ),
        content_checksum=f"sha256:{spec.publication_id}",
        evidence=PublicationEvidence(
            rule_set_version="historical-unit-test",
            legacy=LegacyPublicationRules(
                passed_rules=("historical_success",),
                qualified_warnings=spec.qualified_dq,
            ),
        ),
        fencing_token=1,
        published_at=spec.published_at,
    )


def bound_plan(
    logical: QueryPlan,
    published_at: datetime,
    qualified_dq: tuple[str, ...] = (),
) -> BoundQueryPlan:
    """Bind every requested day to deterministic immutable publication identities."""

    def publications(dataset: str, side: str) -> tuple[SuccessfulPublication, ...]:
        return tuple(
            successful_publication(
                dataset,
                logical.window.start + timedelta(days=offset),
                _PublicationSpec(
                    publication_id=(
                        f"unit-{side}-{dataset}-"
                        f"{(logical.window.start + timedelta(days=offset)).isoformat()}"
                    ),
                    published_at=published_at,
                    qualified_dq=qualified_dq,
                ),
            )
            for offset in range(logical.window.days)
        )

    return BoundQueryPlan(
        logical=logical,
        primary=publications(logical.fact, "primary"),
        primary_filters=tuple(sorted(logical.filters.items())),
        joined=(() if logical.join is None else publications(logical.join.fact, "joined")),
        joined_filters=(
            () if logical.join is None else tuple(sorted(logical.join.filters.items()))
        ),
    )


def _default_coverage(request: PublicationSelectionRequest) -> Coverage:
    ordered = sorted(
        {plan.window for plan in request.plans},
        key=lambda item: (item.start, item.end),
    )
    intervals: list[DateRange] = [ordered[0]]
    for following in ordered[1:]:
        previous = intervals[-1]
        if following.start <= previous.end + timedelta(days=1):
            intervals[-1] = DateRange(previous.start, max(previous.end, following.end))
        else:
            intervals.append(following)
    exact = tuple(intervals)
    return Coverage(
        fact_name=" + ".join(sorted({fact for plan in request.plans for fact in plan.facts_read})),
        window=exact[0] if len(exact) == 1 else None,
        freshness=FreshnessStatus.FRESH,
        measurement_basis=request.plans[0].measurement_basis[0],
        valid_grains=request.plans[0].metric.valid_grains,
        loaded_at=request.now - timedelta(hours=2),
        intervals=exact,
    )


@dataclass
class StubPublications:
    """Record selection or inspection and bind plans in their original request order."""

    coverage: Coverage | None = None
    qualified_dq: tuple[str, ...] = ()
    failure: Exception | None = None
    inspection_absent_indices: frozenset[int] = frozenset()
    inspection_coverage_by_index: dict[int, Coverage] = field(default_factory=dict[int, Coverage])
    seen: list[PublicationSelectionRequest] = field(
        default_factory=list[PublicationSelectionRequest]
    )
    inspections: list[PresentationEvidenceInspectionRequest] = field(
        default_factory=list[PresentationEvidenceInspectionRequest]
    )

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        self.seen.append(request)
        if self.failure is not None:
            raise self.failure
        published_at = request.now - timedelta(hours=2)
        plans = tuple(
            bound_plan(
                logical,
                published_at,
                self.qualified_dq,
            )
            for logical in request.plans
        )
        return PublicationSelection(
            plans=plans,
            coverage=self.coverage or _default_coverage(request),
            qualified_dq=self.qualified_dq,
        )

    def inspect(
        self,
        request: PresentationEvidenceInspectionRequest,
    ) -> PresentationEvidenceInspectionResult:
        self.inspections.append(request)
        if self.failure is not None:
            raise self.failure
        published_at = request.now - timedelta(hours=2)
        slots: list[PresentationEvidenceInspectionSlot] = []
        for index, requested in enumerate(request.plans):
            if index in self.inspection_absent_indices:
                slots.append(PresentationEvidenceInspectionSlot(requested.role, None))
                continue
            bound = bound_plan(requested.plan, published_at, self.qualified_dq)
            selection_request = PublicationSelectionRequest(
                (requested.plan,),
                request.contracts,
                request.now,
            )
            slots.append(
                PresentationEvidenceInspectionSlot(
                    requested.role,
                    bound,
                    self.inspection_coverage_by_index.get(index)
                    or self.coverage
                    or _default_coverage(selection_request),
                    self.qualified_dq,
                )
            )
        return PresentationEvidenceInspectionResult(tuple(slots))


PublicationSpec = _PublicationSpec
"""Public alias for tests that build standalone historical publications."""
