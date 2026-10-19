# SPDX-License-Identifier: MIT-0
"""Semantic V2 pre-conformance, DQ, reconciliation, and exact suppression gates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.outbound.source_pack import load_source_packs
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.prepublication import (
    CandidateMeasureTotal,
    ControlComparisonKey,
    ControlComparisonScope,
    ControlTotalRequest,
    PrepublicationRequest,
    ReportedMeasureTotal,
    SourcePackConformanceRequest,
    SourcePackConformanceResult,
    SuppressionCoverageEvidence,
    WithheldResidualTotal,
    conform_source_pack,
    evaluate_control_total,
    evaluate_prepublication,
    evaluate_suppression_coverage,
)
from domain.semantics.publication import (
    CanonicalDatasetKey,
    PartitionIdentity,
    PublicationLensKey,
    PublicationScope,
    SourcePublicationRef,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_pack import (
    PackReadiness,
    SourcePackBlocker,
    SourcePackBlockerFamily,
)
from domain.semantics.time import (
    BusinessCalendarKey,
    FactTimeClass,
    MeasurementBucket,
    TimeGrain,
)
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    PublicationEvidenceSubject,
    RuleOutcome,
)
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-049")]
ROOT = Path(__file__).resolve().parents[2]
CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def _bucket(day: int) -> MeasurementBucket:
    selected = date(2026, 8, day)
    return MeasurementBucket(
        window=DateRange(selected, selected),
        grain=TimeGrain.DAY,
        calendar=CALENDAR,
        source_timezone="PST",
    )


def _partition(day: int = 25, *, lens: str = "default") -> PartitionIdentity:
    return PartitionIdentity(
        scope=PublicationScope(
            dataset=CanonicalDatasetKey("fct_retail_sales"),
            commerce_scope=CommerceScopeKey("amazon_us"),
            population_key="manufacturing_retail",
            lens=PublicationLensKey(lens),
            time_class=FactTimeClass.DAILY_ADDITIVE,
            business_calendar=CALENDAR,
            source_timezone="PST",
        ),
        bucket=_bucket(day),
    )


def _result(
    family: PrepublicationRuleFamily,
    outcome: RuleOutcome = RuleOutcome.PASSED,
    qualifier: str | None = None,
) -> PrepublicationRuleResult:
    return PrepublicationRuleResult(
        family=family,
        rule_id=f"test:{family.value}",
        outcome=outcome,
        detail="fixture check completed",
        qualifier=qualifier,
    )


def _conformance(contract_id: str = "vendor-sales-by-asin"):
    pack = next(pack for pack in load_source_packs(ROOT) if pack.contract_id == contract_id)
    return conform_source_pack(
        SourcePackConformanceRequest(
            pack=pack,
            source_publication=SourcePublicationRef(f"source:{contract_id}"),
            results=tuple(
                replace(_result(family), rule_id=f"{contract_id}:{family.value}")
                for family in (
                    PrepublicationRuleFamily.PROVENANCE,
                    PrepublicationRuleFamily.IDENTITY,
                    PrepublicationRuleFamily.TIME,
                )
            ),
        )
    )


def _subject(contract_id: str = "vendor-sales-by-asin") -> PublicationEvidenceSubject:
    return PublicationEvidenceSubject(
        publisher="canonical-curation",
        partition=_partition().identity,
        source_publications=(f"source:{contract_id}",),
        source_revision="test.control_total|instant|2026-08-25T10:00:00+00:00|revision-1",
        content_checksum="sha256:candidate",
    )


def _candidate_results() -> tuple[PrepublicationRuleResult, ...]:
    return tuple(
        _result(family)
        for family in PrepublicationRuleFamily
        if family
        not in {
            PrepublicationRuleFamily.PROVENANCE,
            PrepublicationRuleFamily.IDENTITY,
            PrepublicationRuleFamily.TIME,
        }
    )


def _revision(source_id: str = "revision-1") -> SourceRevision:
    return SourceRevision.instant(
        scheme=RevisionSchemeKey("test.control_total"),
        source_id=source_id,
        observed_at=NOW,
    )


@dataclass(frozen=True, slots=True)
class _ControlKeySpec:
    partition: PartitionIdentity | None = None
    revision: SourceRevision | None = None
    basis: MeasurementBasis = MeasurementBasis.RETAIL_ACTUAL
    unit: str = "count"


def _control_key(spec: _ControlKeySpec | None = None) -> ControlComparisonKey:
    selected = spec or _ControlKeySpec()
    partition = selected.partition or _partition()
    scope = partition.scope
    return ControlComparisonKey(
        scope=ControlComparisonScope(
            group="vendor-sales",
            commerce_scope=scope.commerce_scope,
            population_key=scope.population_key,
            lens=scope.lens,
            time_class=scope.time_class,
            bucket=partition.bucket,
            delivery_identity="manifest-v1|object-v1",
        ),
        source_revision=selected.revision or _revision(),
        measurement_basis=selected.basis,
        unit=selected.unit,
    )


class TestTypedGateEvidence:
    def test_complete_gate_preserves_typed_results(self) -> None:
        decision = evaluate_prepublication(
            PrepublicationRequest(
                rule_set_version="v2.1",
                subject=_subject(),
                source_conformance=(_conformance(),),
                candidate_results=_candidate_results(),
            )
        )

        assert decision.publishable
        assert {result.family for result in decision.evidence.results} == set(
            PrepublicationRuleFamily
        )

    def test_missing_family_fails_closed(self) -> None:
        incomplete = tuple(
            result
            for result in _candidate_results()
            if result.family is not PrepublicationRuleFamily.TABLE_DQ
        )
        with pytest.raises(ValueError, match="table_dq"):
            evaluate_prepublication(
                PrepublicationRequest(
                    rule_set_version="v2.1",
                    subject=_subject(),
                    source_conformance=(_conformance(),),
                    candidate_results=incomplete,
                )
            )

    def test_qualified_rule_requires_and_preserves_its_caveat(self) -> None:
        qualified = _result(
            PrepublicationRuleFamily.TABLE_DQ,
            outcome=RuleOutcome.QUALIFIED,
            qualifier="DQ passed under the declared sparse-column rule.",
        )
        candidates = tuple(
            qualified if result.family is PrepublicationRuleFamily.TABLE_DQ else result
            for result in _candidate_results()
        )
        decision = evaluate_prepublication(
            PrepublicationRequest(
                rule_set_version="v2.1",
                subject=_subject(),
                source_conformance=(_conformance(),),
                candidate_results=candidates,
            )
        )

        assert decision.publishable
        assert decision.evidence.qualified_warnings == (
            "DQ passed under the declared sparse-column rule.",
        )

    def test_unqualified_warning_cannot_be_constructed(self) -> None:
        with pytest.raises(ValueError, match="mandatory caveat"):
            _result(PrepublicationRuleFamily.TABLE_DQ, outcome=RuleOutcome.QUALIFIED)

    def test_publication_evidence_cannot_repeat_a_rule(self) -> None:
        results = tuple(
            replace(_result(family), rule_id=family.value) for family in PrepublicationRuleFamily
        )
        with pytest.raises(ValueError, match="repeats"):
            PublicationEvidence(
                rule_set_version="v2.1",
                subject=_subject(),
                results=(*results, results[0]),
            )


class TestSourcePackConformance:
    def test_every_resolved_pack_conformance_is_publishable(self) -> None:
        packs = load_source_packs(ROOT)
        assert packs
        assert all(_conformance(pack.contract_id).publishable for pack in packs)

    def test_blocked_result_cannot_be_fabricated_as_all_passed(self) -> None:
        ready = next(
            pack for pack in load_source_packs(ROOT) if pack.contract_id == "amc-dsp-clicks"
        )
        pack = replace(
            ready,
            readiness=PackReadiness.BLOCKED,
            blocker=SourcePackBlocker(
                SourcePackBlockerFamily.IDENTITY,
                "synthetic unresolved identity evidence",
            ),
        )
        with pytest.raises(ValueError, match="must fail its identity"):
            SourcePackConformanceResult(
                pack=pack,
                source_publication=SourcePublicationRef("source:fabricated"),
                results=tuple(
                    _result(family)
                    for family in (
                        PrepublicationRuleFamily.PROVENANCE,
                        PrepublicationRuleFamily.IDENTITY,
                        PrepublicationRuleFamily.TIME,
                    )
                ),
            )

    def test_source_conformance_requires_each_source_family_once(self) -> None:
        pack = next(
            pack for pack in load_source_packs(ROOT) if pack.readiness is PackReadiness.READY
        )
        with pytest.raises(ValueError, match="provenance, identity, and time"):
            SourcePackConformanceRequest(
                pack=pack,
                source_publication=SourcePublicationRef("source:incomplete"),
                results=(_result(PrepublicationRuleFamily.IDENTITY),),
            )


class TestResidualAwareControlTotals:
    def test_attributable_plus_matching_residual_reconciles(self) -> None:
        key = _control_key()
        result = evaluate_control_total(
            ControlTotalRequest(
                candidate=CandidateMeasureTotal(
                    "units_shipped",
                    key,
                    Decimal("90"),
                    WithheldResidualTotal(key, Decimal("10")),
                    True,
                ),
                reported=ReportedMeasureTotal("units_shipped", key, Decimal("100")),
            )
        )

        assert result.outcome is RuleOutcome.PASSED

    def test_observed_suppression_without_residual_fails(self) -> None:
        key = _control_key()
        result = evaluate_control_total(
            ControlTotalRequest(
                candidate=CandidateMeasureTotal("units_shipped", key, Decimal("90"), None, True),
                reported=ReportedMeasureTotal("units_shipped", key, Decimal("90")),
            )
        )

        assert result.outcome is RuleOutcome.FAIL
        assert "residual" in result.detail

    def test_residual_from_another_exact_scope_cannot_reconcile(self) -> None:
        key = _control_key()
        other = _control_key(_ControlKeySpec(partition=_partition(lens="another_lens")))
        result = evaluate_control_total(
            ControlTotalRequest(
                candidate=CandidateMeasureTotal(
                    "units_shipped",
                    key,
                    Decimal("90"),
                    WithheldResidualTotal(other, Decimal("10")),
                    True,
                ),
                reported=ReportedMeasureTotal("units_shipped", key, Decimal("100")),
            )
        )

        assert result.outcome is RuleOutcome.FAIL
        assert "Withheld residual" in result.detail

    @pytest.mark.parametrize(
        "reported_key",
        [
            _control_key(_ControlKeySpec(partition=_partition(lens="another_lens"))),
            _control_key(_ControlKeySpec(partition=_partition(day=24))),
            _control_key(_ControlKeySpec(revision=_revision("revision-2"))),
            _control_key(_ControlKeySpec(basis=MeasurementBasis.AD_ATTRIBUTED)),
            _control_key(_ControlKeySpec(unit="currency:USD")),
        ],
    )
    def test_incompatible_scope_bucket_revision_basis_or_unit_cannot_reconcile(
        self,
        reported_key: ControlComparisonKey,
    ) -> None:
        candidate_key = _control_key()
        result = evaluate_control_total(
            ControlTotalRequest(
                candidate=CandidateMeasureTotal(
                    "units_shipped", candidate_key, Decimal("100"), None, False
                ),
                reported=ReportedMeasureTotal("units_shipped", reported_key, Decimal("100")),
            )
        )

        assert result.outcome is RuleOutcome.FAIL
        assert "exact bucket" in result.detail

    def test_residual_omission_cannot_hide_a_mismatch(self) -> None:
        key = _control_key()
        result = evaluate_control_total(
            ControlTotalRequest(
                candidate=CandidateMeasureTotal(
                    "units_shipped",
                    key,
                    Decimal("90"),
                    WithheldResidualTotal(key, Decimal("0")),
                    False,
                ),
                reported=ReportedMeasureTotal("units_shipped", key, Decimal("100")),
            )
        )

        assert result.outcome is RuleOutcome.FAIL
        assert "90" in result.detail and "100" in result.detail


class TestExactSuppressionCoverage:
    def test_suppressed_middle_bucket_remains_an_exact_hole(self) -> None:
        evidence = SuppressionCoverageEvidence(
            requested_buckets=(_bucket(1), _bucket(2), _bucket(3)),
            unavailable_buckets=(_bucket(2),),
        )

        assert evidence.proven_buckets == (_bucket(1), _bucket(3))
        assert evaluate_suppression_coverage(_partition(1), evidence).outcome is RuleOutcome.PASSED
        assert evaluate_suppression_coverage(_partition(2), evidence).outcome is RuleOutcome.FAIL
        assert evaluate_suppression_coverage(_partition(3), evidence).outcome is RuleOutcome.PASSED

    def test_unassignable_multi_day_residual_cannot_widen_to_the_envelope(self) -> None:
        requested = (_bucket(1), _bucket(2), _bucket(3))
        evidence = SuppressionCoverageEvidence(
            requested_buckets=requested,
            unavailable_buckets=requested,
        )

        assert evidence.proven_buckets == ()
        assert all(
            evaluate_suppression_coverage(_partition(day), evidence).outcome is RuleOutcome.FAIL
            for day in (1, 2, 3)
        )

    def test_unrequested_bucket_cannot_be_marked_as_a_hole(self) -> None:
        with pytest.raises(ValueError, match="unrequested"):
            SuppressionCoverageEvidence(
                requested_buckets=(_bucket(1),),
                unavailable_buckets=(_bucket(2),),
            )
