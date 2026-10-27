# SPDX-License-Identifier: MIT-0
"""Semantic V2 publication selection and publication-backed capability coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import adapters.outbound.publication_selection as selection_module
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.insight_precompute.planning import campaign_coverage_requirements
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    new_customers_coverage_requirements,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    retail_health_coverage_requirements,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    scorecard_coverage_requirements,
)
from adapters.outbound.catalog import (
    MetricWindowDefaults,
    PageDefaultWindowCompleteness,
    PageDefaultWindowRequest,
    PageWindowCatalog,
    PageWindowRequirement,
    PublicationCatalog,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    MAX_PRESET_SNAPSHOT_PARTITIONS,
    PerPackPresentationEvidenceInspection,
    PublicationScopeRequest,
    PublicationSelector,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.telemetry.reporting import Report
from domain.catalog.capability import Catalog
from domain.data_contracts.contract import DataContract, FieldMapping
from domain.measurement.pack_coverage import PackDayVersions
from domain.measurement.planning import FactShape, FactShapes, PlanRequest, QueryPlan, plan
from domain.operations.bake_refusals import IsolatedRefusalReason, classify_refusal
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.identity import (
    EcosystemKey,
    IdentityMapping,
    MarketplaceKey,
    ProductKey,
    SourceIdentifier,
)
from domain.semantics.media_taxonomy import MediaTaxonomyTerm
from domain.semantics.publication import (
    AttemptId,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationId,
    PublicationScope,
    SourcePublicationRef,
    SuccessfulPublication,
    publication_lens_for,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_pack import SourcePack
from domain.semantics.time import FactTimeClass, MeasurementBucket, TimeGrain
from domain.semantics.validation import LegacyPublicationRules, PublicationEvidence
from domain.shared_kernel.errors import DependencyError, DomainError, ErrorCode, ValidationError
from domain.shared_kernel.provenance import FreshnessStatus
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceRole,
    PublicationSelectionRequest,
)
from ports.outbound.semantic_state import (
    MAX_EXACT_CURRENT_PARTITIONS,
    InspectExactResult,
)
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageRequest,
    SourceCoverageRequirement,
    SourceCoverageResult,
    metric_coverage_requirement,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-044", "ADR-045", "ADR-046")]

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
DAY_ONE = date(2026, 8, 10)
ONE_POPULATION = {
    "marketplace_key": "ATVPDKIKX0DER",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}
_PUBLISHER = CanonicalPublisherKey("unit-publication-selector")


@dataclass
class RecordingCurrent:
    """In-memory implementation of the read-only CURRENT publication port."""

    exact_available: bool = True
    exact_absent_datasets: frozenset[str] = frozenset()
    exact_warnings: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    scope_publications: dict[str, tuple[SuccessfulPublication, ...]] = field(
        default_factory=dict[str, tuple[SuccessfulPublication, ...]]
    )
    exact_calls: list[tuple[PartitionIdentity, ...]] = field(
        default_factory=list[tuple[PartitionIdentity, ...]]
    )
    scope_calls: list[PublicationScope] = field(default_factory=list[PublicationScope])
    earliest_calls: list[PublicationScope] = field(default_factory=list[PublicationScope])

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        return next(
            (
                item
                for item in self.scope_publications.get(partition.scope.identity, ())
                if item.partition == partition
            ),
            None,
        )

    def inspect_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> InspectExactResult:
        self.exact_calls.append(partitions)
        return InspectExactResult(
            tuple(
                None
                if (
                    not self.exact_available
                    or partition.scope.dataset.value in self.exact_absent_datasets
                )
                else _success(
                    partition,
                    published_at=NOW - timedelta(hours=1),
                    warnings=self.exact_warnings.get(partition.identity, ()),
                )
                for partition in partitions
            )
        )

    def current_exact(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> tuple[SuccessfulPublication, ...] | None:
        inspected = self.inspect_exact(partitions)
        if any(publication is None for publication in inspected.publications):
            return None
        return tuple(
            publication for publication in inspected.publications if publication is not None
        )

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon
        self.scope_calls.append(scope)
        return self.scope_publications.get(scope.identity, ())

    def earliest_current_for_scope(self, scope: PublicationScope) -> SuccessfulPublication | None:
        self.earliest_calls.append(scope)
        return min(
            self.scope_publications.get(scope.identity, ()),
            key=lambda item: item.partition.bucket.window.start,
            default=None,
        )

    def load_scope(
        self,
        scope: PublicationScope,
        intervals: tuple[DateRange, ...],
        *,
        published_at: datetime = NOW - timedelta(hours=1),
    ) -> None:
        days = tuple(
            DateRange(window.start + timedelta(days=offset), window.start + timedelta(days=offset))
            for window in intervals
            for offset in range(window.days)
        )
        partitions = tuple(
            PartitionIdentity(
                scope=scope,
                bucket=MeasurementBucket(
                    window=window,
                    grain=TimeGrain.DAY,
                    calendar=scope.business_calendar,
                    source_timezone=scope.source_timezone,
                    observation_basis=scope.observation_basis,
                ),
            )
            for window in days
        )
        self.scope_publications[scope.identity] = tuple(
            _success(partition, published_at=published_at) for partition in partitions
        )


def _success(
    partition: PartitionIdentity,
    *,
    published_at: datetime,
    warnings: tuple[str, ...] = (),
) -> SuccessfulPublication:
    token = "-".join(
        (
            partition.scope.dataset.value,
            partition.scope.lens.value,
            partition.bucket.window.start.isoformat(),
            partition.bucket.window.end.isoformat(),
        )
    )
    source_id = f"source-{token}"
    return SuccessfulPublication(
        publication_id=PublicationId(f"publication-{token}"),
        publisher=_PUBLISHER,
        partition=partition,
        attempt_id=AttemptId(f"attempt-{token}"),
        source_inputs=(SourcePublicationRef(source_id),),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("unit.publication_selection"),
            source_id=source_id,
            observed_at=published_at,
        ),
        content_checksum=f"sha256:{token}",
        evidence=PublicationEvidence(
            rule_set_version="publication-selection-unit",
            legacy=LegacyPublicationRules(
                passed_rules=("historical_success",),
                qualified_warnings=warnings,
            ),
        ),
        fencing_token=1,
        published_at=published_at,
    )


@dataclass(frozen=True, slots=True)
class PublicationEnvironment:
    """Real committed declarations shared by the direct publication tests."""

    contracts: tuple[DataContract, ...]
    contracts_by_id: dict[str, DataContract]
    packs: tuple[SourcePack, ...]
    packs_by_id: dict[str, SourcePack]
    bridge: CommerceIdentityBridge
    metric_catalog: Catalog


@pytest.fixture(scope="module")
def environment() -> PublicationEnvironment:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    packs = load_source_packs(ROOT)
    return PublicationEnvironment(
        contracts=contracts,
        contracts_by_id={contract.contract_id: contract for contract in contracts},
        packs=packs,
        packs_by_id={pack.contract_id: pack for pack in packs},
        bridge=load_commerce_identity_bridge(ROOT),
        metric_catalog=load_catalog(ROOT),
    )


def _sales_plan(environment: PublicationEnvironment, window: DateRange) -> QueryPlan:
    return plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=window,
            filters=ONE_POPULATION,
        ),
        FactShape.of(environment.contracts_by_id["vendor-sales-by-asin"]),
    )


def _conversion_plan(environment: PublicationEnvironment, window: DateRange) -> QueryPlan:
    return plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name="conversion_rate_retail",
            grains=("product",),
            window=window,
            filters=ONE_POPULATION,
        ),
        FactShapes(
            numerator=FactShape.for_fact("fct_retail_sales", environment.contracts),
            denominator=FactShape.for_fact("fct_product_page_views", environment.contracts),
        ),
    )


def _scope(contract_id: str, environment: PublicationEnvironment) -> PublicationScope:
    return publication_scopes_for(
        PublicationScopeRequest(
            pack=environment.packs_by_id[contract_id],
            contract=environment.contracts_by_id[contract_id],
            bridge=environment.bridge,
            filters={},
        )
    )[0]


def test_serving_reconstructs_the_publishers_profile_and_vendor_options(
    environment: PublicationEnvironment,
) -> None:
    scope = publication_scopes_for(
        PublicationScopeRequest(
            pack=environment.packs_by_id["vendor-sales-by-asin"],
            contract=environment.contracts_by_id["vendor-sales-by-asin"],
            bridge=environment.bridge,
            filters=ONE_POPULATION,
        )
    )[0]

    expected_options = (
        ("distributorView", json.dumps("MANUFACTURING")),
        ("reportPeriod", json.dumps("DAY")),
        ("sellingProgram", json.dumps("RETAIL")),
    )
    assert scope.population_key == "amazon_us"
    assert scope.lens == publication_lens_for(
        "source_pack.vendor_sales_by_asin", expected_options, 2
    )
    assert scope.lens != publication_lens_for(
        "source_pack.vendor_sales_by_asin",
        tuple(
            (name, json.dumps("WEEK") if name == "reportPeriod" else value)
            for name, value in expected_options
        ),
        2,
    )


def test_a_bare_asi_segment_marketplace_value_is_outside_the_publication_scope(
    environment: PublicationEnvironment,
) -> None:
    """Review B-1: '1' is an `amc.segment_marketplace_id` alias, never a bare marketplace."""
    request = PublicationScopeRequest(
        pack=environment.packs_by_id["vendor-sales-by-asin"],
        contract=environment.contracts_by_id["vendor-sales-by-asin"],
        bridge=environment.bridge,
        filters=ONE_POPULATION | {"marketplace_key": "1"},
    )

    with pytest.raises(ValueError, match=r"marketplace_key '1' is outside the governed scope"):
        publication_scopes_for(request)
    control = publication_scopes_for(replace(request, filters=ONE_POPULATION))[0]
    assert control.population_key == "amazon_us"


def test_a_bare_asi_segment_marketplace_value_is_refused_as_a_serving_filter(
    environment: PublicationEnvironment,
) -> None:
    """Review B-1: serving's source-filter lookup ignores query-parameter namespaces."""
    selector = PublicationSelector(RecordingCurrent(), environment.packs, environment.bridge)
    window = DateRange(DAY_ONE, DAY_ONE + timedelta(days=1))
    logical = _sales_plan(environment, window)
    bare = plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=window,
            filters=ONE_POPULATION | {"marketplace_key": "1"},
        ),
        FactShape.of(environment.contracts_by_id["vendor-sales-by-asin"]),
    )

    with pytest.raises(ValueError, match="must resolve throughout the request window"):
        selector.select(PublicationSelectionRequest((bare,), environment.contracts, NOW))
    control = selector.select(PublicationSelectionRequest((logical,), environment.contracts, NOW))
    assert dict(control.plans[0].primary_filters)["marketplace_key"] == "amazon_us"


def test_product_filter_is_canonicalized_with_the_selected_marketplace(
    environment: PublicationEnvironment,
) -> None:
    logical = plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=DateRange(DAY_ONE, DAY_ONE),
            filters={**ONE_POPULATION, "amazon_asin": "B0FIXTUR01"},
        ),
        FactShape.of(environment.contracts_by_id["vendor-sales-by-asin"]),
    )
    selection = PublicationSelector(
        RecordingCurrent(), environment.packs, environment.bridge
    ).select(PublicationSelectionRequest((logical,), environment.contracts, NOW))

    assert dict(selection.plans[0].primary_filters)["amazon_asin"] == ("amazon_asin:b0fixtur01")


def test_canonical_product_filter_from_prior_tool_result_is_preserved(
    environment: PublicationEnvironment,
) -> None:
    logical = plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=DateRange(DAY_ONE, DAY_ONE),
            filters={
                **ONE_POPULATION,
                "amazon_asin": "amazon_asin:b0fixtur01",
            },
        ),
        FactShape.of(environment.contracts_by_id["vendor-sales-by-asin"]),
    )
    selection = PublicationSelector(
        RecordingCurrent(), environment.packs, environment.bridge
    ).select(PublicationSelectionRequest((logical,), environment.contracts, NOW))

    assert dict(selection.plans[0].primary_filters)["amazon_asin"] == ("amazon_asin:b0fixtur01")


def test_daily_source_product_filter_must_resolve_to_one_product_for_every_day(
    environment: PublicationEnvironment,
) -> None:
    marketplace = MarketplaceKey("amazon_us")
    source = SourceIdentifier("vendor.asin", "B0FIXTUR01")
    bridge = replace(
        environment.bridge,
        product_mappings=(
            IdentityMapping(
                source,
                ProductKey("amazon_asin:before", environment.bridge.commerce_scope, marketplace),
                1,
                DAY_ONE,
                DAY_ONE + timedelta(days=1),
            ),
            IdentityMapping(
                source,
                ProductKey("amazon_asin:after", environment.bridge.commerce_scope, marketplace),
                2,
                DAY_ONE + timedelta(days=1),
            ),
        ),
        product_rules=tuple(
            rule
            for rule in environment.bridge.product_rules
            if rule.source_namespace != "vendor.asin"
        ),
    )
    logical = plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name="retail_revenue_shipped",
            grains=("product",),
            window=DateRange(DAY_ONE, DAY_ONE + timedelta(days=1)),
            filters={**ONE_POPULATION, "amazon_asin": "B0FIXTUR01"},
        ),
        FactShape.of(environment.contracts_by_id["vendor-sales-by-asin"]),
    )
    current = RecordingCurrent()

    with pytest.raises(ValueError, match="one canonical product"):
        PublicationSelector(current, environment.packs, bridge).select(
            PublicationSelectionRequest((logical,), environment.contracts, NOW)
        )

    assert current.exact_calls == []


def test_detail_and_reported_profiles_cannot_collide(
    environment: PublicationEnvironment,
) -> None:
    lenses = {
        contract_id: publication_scopes_for(
            PublicationScopeRequest(
                pack=environment.packs_by_id[contract_id],
                contract=environment.contracts_by_id[contract_id],
                bridge=environment.bridge,
                filters=ONE_POPULATION,
            )
        )[0].lens
        for contract_id in ("vendor-sales-by-asin", "vendor-sales-reported-total")
    }

    assert lenses["vendor-sales-by-asin"] != lenses["vendor-sales-reported-total"]


def test_selection_reads_every_required_partition_in_one_sorted_transaction(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    later = _sales_plan(
        environment,
        DateRange(DAY_ONE + timedelta(days=8), DAY_ONE + timedelta(days=9)),
    )
    earlier = _sales_plan(
        environment,
        DateRange(DAY_ONE, DAY_ONE + timedelta(days=1)),
    )

    selection = selector.select(
        PublicationSelectionRequest((later, earlier), environment.contracts, NOW)
    )

    assert len(current.exact_calls) == 1
    requested = current.exact_calls[0]
    assert tuple(item.identity for item in requested) == tuple(
        sorted(item.identity for item in requested)
    )
    assert {item.bucket.grain for item in requested} == {TimeGrain.DAY}
    assert {item.bucket.window for item in requested} == {
        DateRange(DAY_ONE, DAY_ONE),
        DateRange(DAY_ONE + timedelta(days=1), DAY_ONE + timedelta(days=1)),
        DateRange(DAY_ONE + timedelta(days=8), DAY_ONE + timedelta(days=8)),
        DateRange(DAY_ONE + timedelta(days=9), DAY_ONE + timedelta(days=9)),
    }
    assert selection.coverage.intervals == (
        DateRange(DAY_ONE, DAY_ONE + timedelta(days=1)),
        DateRange(DAY_ONE + timedelta(days=8), DAY_ONE + timedelta(days=9)),
    )
    assert dict(selection.plans[0].primary_filters)["marketplace_key"] == "amazon_us"
    assert selection.coverage.covers_all((earlier.window, later.window))


def test_inspection_returns_ordered_role_slots_from_one_aligned_partial_read(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent(exact_absent_datasets=frozenset({"fct_product_page_views"}))
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    current_plan = _conversion_plan(
        environment,
        DateRange(DAY_ONE + timedelta(days=8), DAY_ONE + timedelta(days=8)),
    )
    comparator_plan = _sales_plan(environment, DateRange(DAY_ONE, DAY_ONE))

    inspected = selector.inspect(
        PresentationEvidenceInspectionRequest(
            plans=(
                PresentationEvidenceInspectionPlan(
                    PresentationEvidenceRole.CURRENT,
                    current_plan,
                ),
                PresentationEvidenceInspectionPlan(
                    PresentationEvidenceRole.COMPARATOR,
                    comparator_plan,
                ),
            ),
            contracts=environment.contracts,
            now=NOW,
        )
    )

    assert len(current.exact_calls) == 1
    requested = current.exact_calls[0]
    assert tuple(partition.identity for partition in requested) == tuple(
        sorted(partition.identity for partition in requested)
    )
    assert {partition.scope.dataset.value for partition in requested} == {
        "fct_product_page_views",
        "fct_retail_sales",
    }
    assert tuple(slot.role for slot in inspected.slots) == (
        PresentationEvidenceRole.CURRENT,
        PresentationEvidenceRole.COMPARATOR,
    )
    assert not inspected.slots[0].present
    assert inspected.slots[0].bound is None
    assert inspected.slots[0].coverage is None
    assert inspected.slots[0].qualified_dq == ()
    assert inspected.slots[1].present
    assert inspected.slots[1].bound is not None
    assert inspected.slots[1].bound.logical == comparator_plan
    assert inspected.slots[1].coverage is not None
    assert inspected.slots[1].coverage.intervals == (comparator_plan.window,)


def test_inspection_enforces_partition_limit_before_current_read(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    oversized = _sales_plan(environment, DateRange(DAY_ONE, DAY_ONE + timedelta(days=1_000)))

    with pytest.raises(ValidationError) as raised:
        PublicationSelector(current, environment.packs, environment.bridge).inspect(
            PresentationEvidenceInspectionRequest(
                plans=(
                    PresentationEvidenceInspectionPlan(
                        PresentationEvidenceRole.CURRENT,
                        oversized,
                    ),
                ),
                contracts=environment.contracts,
                now=NOW,
            )
        )

    assert str(raised.value.code) == "ADLY-PUBLICATION_SELECTION-0002"
    assert current.exact_calls == []
    # ADR-142: at a bake this is its page's partitions_over_bound, carrying both figures.
    refusal = classify_refusal(raised.value)
    assert refusal.reason is IsolatedRefusalReason.PARTITIONS_OVER_BOUND
    assert dict(refusal.refusal_context) == {
        "maximum_partitions": 1_000,
        "required_partitions": 1_001,
    }


def test_selection_remains_all_or_none_when_one_inspection_partition_is_absent(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent(exact_absent_datasets=frozenset({"fct_product_page_views"}))
    logical = _conversion_plan(environment, DateRange(DAY_ONE, DAY_ONE))

    with pytest.raises(DomainError, match="None of the requested days are published"):
        PublicationSelector(current, environment.packs, environment.bridge).select(
            PublicationSelectionRequest((logical,), environment.contracts, NOW)
        )

    # One gating read plus one diagnostic read that names the covered days in the refusal.
    assert len(current.exact_calls) == 2


@pytest.mark.covers("ADR-070")
def test_more_partitions_than_the_exact_read_bound_is_validation_before_current_read(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    oversized = _sales_plan(environment, DateRange(DAY_ONE, DAY_ONE + timedelta(days=1_000)))

    with pytest.raises(ValidationError) as raised:
        selector.select(PublicationSelectionRequest((oversized,), environment.contracts, NOW))

    assert str(raised.value.code) == "ADLY-PUBLICATION_SELECTION-0002"
    assert raised.value.context == {"required_partitions": 1_001, "maximum_partitions": 1_000}
    assert not raised.value.alarms
    assert current.exact_calls == []
    # Paired: a quarter's 184 day partitions, over the former 100, bind in one exact read.
    quarter = _sales_plan(environment, DateRange(DAY_ONE, DAY_ONE + timedelta(days=183)))
    selected = selector.select(PublicationSelectionRequest((quarter,), environment.contracts, NOW))
    assert len(current.exact_calls) == 1 and len(current.exact_calls[0]) == 184
    assert len(selected.plans[0].all_publications) == 184


def test_missing_current_pointer_fails_before_any_plan_can_cross_the_query_boundary(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent(exact_available=False)
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    plan_to_bind = _sales_plan(
        environment,
        DateRange(DAY_ONE, DAY_ONE),
    )
    query_boundary: list[QueryPlan] = []

    with pytest.raises(DomainError, match="No published data covers the requested window"):
        selection = selector.select(
            PublicationSelectionRequest((plan_to_bind,), environment.contracts, NOW)
        )
        query_boundary.extend(item.logical for item in selection.plans)

    assert len(current.exact_calls) == 2
    assert query_boundary == []


def test_a_failed_coverage_diagnosis_is_reported_and_the_refusal_is_unchanged(
    environment: PublicationEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal never depends on the diagnosis, but the diagnosis failure is not discarded."""
    current = RecordingCurrent()
    unreadable = DependencyError(ErrorCode("SEMANTIC_STATE", 99), "fixture read failed.")

    def no_current(_partitions: tuple[PartitionIdentity, ...]) -> None:
        return None

    def fail(_partitions: tuple[PartitionIdentity, ...]) -> InspectExactResult:
        raise unreadable

    monkeypatch.setattr(current, "current_exact", no_current)
    monkeypatch.setattr(current, "inspect_exact", fail)
    reported: list[tuple[BaseException, str]] = []

    def capture(error: BaseException, report: Report) -> None:
        reported.append((error, report.operation))

    monkeypatch.setattr(selection_module, "report_error", capture)
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    with pytest.raises(DomainError) as raised:
        selector.select(
            PublicationSelectionRequest(
                (_sales_plan(environment, DateRange(DAY_ONE, DAY_ONE)),), environment.contracts, NOW
            )
        )
    assert raised.value.description == (
        "No published data covers the requested window. Ask about a covered period, or omit "
        "the window to use the latest published coverage."
    )
    assert not raised.value.alarms
    assert reported == [(unreadable, "publication_selection.coverage_diagnosis")]


def test_drill_across_binds_primary_and_joined_publication_ids_independently(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    logical = _conversion_plan(
        environment,
        DateRange(DAY_ONE, DAY_ONE + timedelta(days=1)),
    )

    selected = PublicationSelector(current, environment.packs, environment.bridge).select(
        PublicationSelectionRequest((logical,), environment.contracts, NOW)
    )
    bound = selected.plans[0]

    assert bound.logical.join is not None
    assert {item.partition.scope.dataset.value for item in bound.primary} == {"fct_retail_sales"}
    assert {item.partition.scope.dataset.value for item in bound.joined} == {
        "fct_product_page_views"
    }
    assert set(bound.primary_ids).isdisjoint(bound.joined_ids)


def test_adjacent_partitions_coalesce_and_qualified_dq_is_sorted_and_deduplicated(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    logical = _sales_plan(
        environment,
        DateRange(DAY_ONE, DAY_ONE + timedelta(days=2)),
    )
    probe = PublicationSelector(current, environment.packs, environment.bridge)
    probe.select(PublicationSelectionRequest((logical,), environment.contracts, NOW))
    requested = current.exact_calls[0]
    current.exact_warnings = {
        requested[0].identity: ("z-qualified", "shared-qualified"),
        requested[1].identity: ("a-qualified", "shared-qualified"),
    }

    selection = probe.select(PublicationSelectionRequest((logical,), environment.contracts, NOW))

    assert selection.coverage.intervals == (DateRange(DAY_ONE, DAY_ONE + timedelta(days=2)),)
    assert selection.qualified_dq == (
        "a-qualified",
        "shared-qualified",
        "z-qualified",
    )


def test_publication_catalog_uses_scope_reads_and_excludes_non_fact_packs(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()

    coverages = PublicationCatalog(
        current,
        environment.packs,
        environment.contracts,
        environment.bridge,
    ).coverage(NOW)

    queried_datasets = {scope.dataset.value for scope in current.scope_calls}
    assert current.exact_calls == []
    assert "dim_product" not in queried_datasets
    assert "fct_retail_sales_reported_total" not in queried_datasets
    assert "fct_product_page_views_reported_total" not in queried_datasets
    assert {item.fact_name for item in coverages} == {
        "fct_ad_exposure",
        "fct_attributed_conversion",
        "fct_deduplicated_temporal_attribution",
        "fct_exposure_frequency",
        "fct_exposure_group_outcome",
        "fct_new_to_brand_buyers",
        "fct_product_page_views",
        "fct_retail_purchase_amc",
        "fct_retail_sales",
        "fct_segment_delivery",
        "fct_segment_profile",
        "fct_shopper_acquisition",
    }


def test_missing_required_fact_contributor_fails_toward_never_loaded(
    environment: PublicationEnvironment,
) -> None:
    contributor_ids = (
        "amc-reach-frequency",
        "amc-dsp-clicks",
        "amc-dsp-impressions",
        "amc-dsp-views",
    )
    current = RecordingCurrent()
    for contract_id in contributor_ids[:-1]:
        current.load_scope(
            _scope(contract_id, environment),
            (DateRange(DAY_ONE, DAY_ONE),),
        )
    selected_packs = tuple(environment.packs_by_id[item] for item in contributor_ids)
    selected_contracts = tuple(environment.contracts_by_id[item] for item in contributor_ids)

    coverage = PublicationCatalog(
        current, selected_packs, selected_contracts, environment.bridge
    ).coverage_for("fct_ad_exposure", NOW)

    assert coverage is not None
    assert coverage.freshness is FreshnessStatus.NEVER_LOADED
    assert coverage.window is None
    assert coverage.intervals == ()


def test_catalog_intersects_exact_intervals_without_bridging_gaps_and_renders_them(
    environment: PublicationEnvironment,
) -> None:
    contributor_ids = (
        "amc-reach-frequency",
        "amc-dsp-clicks",
        "amc-dsp-impressions",
        "amc-dsp-views",
    )
    windows = {
        contributor_ids[0]: (
            DateRange(DAY_ONE, DAY_ONE + timedelta(days=1)),
            DateRange(DAY_ONE + timedelta(days=4), DAY_ONE + timedelta(days=4)),
        ),
        contributor_ids[1]: (
            DateRange(DAY_ONE + timedelta(days=1), DAY_ONE + timedelta(days=1)),
            DateRange(DAY_ONE + timedelta(days=4), DAY_ONE + timedelta(days=5)),
        ),
        contributor_ids[2]: (
            DateRange(DAY_ONE, DAY_ONE + timedelta(days=2)),
            DateRange(DAY_ONE + timedelta(days=4), DAY_ONE + timedelta(days=6)),
        ),
        contributor_ids[3]: (
            DateRange(DAY_ONE + timedelta(days=1), DAY_ONE + timedelta(days=2)),
            DateRange(DAY_ONE + timedelta(days=3), DAY_ONE + timedelta(days=4)),
        ),
    }
    current = RecordingCurrent()
    for contract_id, intervals in windows.items():
        current.load_scope(
            _scope(contract_id, environment),
            intervals,
        )
    catalog = PublicationCatalog(
        current,
        tuple(environment.packs_by_id[item] for item in contributor_ids),
        tuple(environment.contracts_by_id[item] for item in contributor_ids),
        environment.bridge,
    )

    coverage = catalog.coverage_for("fct_ad_exposure", NOW)

    assert coverage is not None
    assert coverage.freshness is FreshnessStatus.FRESH
    assert coverage.window is None
    assert coverage.intervals == (
        DateRange(DAY_ONE + timedelta(days=1), DAY_ONE + timedelta(days=1)),
        DateRange(DAY_ONE + timedelta(days=4), DAY_ONE + timedelta(days=4)),
    )


def test_catalog_derives_stale_from_the_contributing_contract_threshold(
    environment: PublicationEnvironment,
) -> None:
    contract_id = "vendor-sales-by-asin"
    contract = environment.contracts_by_id[contract_id]
    scope = _scope(contract_id, environment)
    current = RecordingCurrent()
    current.load_scope(
        scope,
        (DateRange(DAY_ONE, DAY_ONE),),
        published_at=NOW - timedelta(hours=contract.staleness_threshold_hours + 1),
    )

    coverage = PublicationCatalog(
        current,
        (environment.packs_by_id[contract_id],),
        (contract,),
        environment.bridge,
    ).coverage_for("fct_retail_sales", NOW)

    assert coverage is not None
    assert coverage.freshness is FreshnessStatus.STALE
    assert coverage.loaded_at == NOW - timedelta(hours=contract.staleness_threshold_hours + 1)


def test_page_window_catalog_intersects_exact_required_source_scopes(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    sales_scope = _scope("vendor-sales-by-asin", environment)
    traffic_scope = _scope("vendor-traffic-by-asin", environment)
    current.load_scope(
        sales_scope,
        (DateRange(date(2026, 8, 1), date(2026, 8, 20)),),
    )
    current.load_scope(
        traffic_scope,
        (DateRange(date(2026, 8, 8), date(2026, 8, 14)),),
    )
    windows = PageWindowCatalog(
        current,
        environment.packs,
        environment.contracts,
        environment.bridge,
    )

    selected = windows.resolve_default(
        PageDefaultWindowRequest(
            requirements=(
                PageWindowRequirement((("vendor_central", "GET_VENDOR_SALES_REPORT"),)),
                PageWindowRequirement((("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"),)),
            ),
            preferred_duration_days=7,
        )
    )

    assert selected is not None
    assert selected.window == DateRange(date(2026, 8, 8), date(2026, 8, 14))
    assert selected.completeness is PageDefaultWindowCompleteness.PREFERRED
    assert {scope.identity for scope in current.scope_calls} == {
        sales_scope.identity,
        traffic_scope.identity,
    }


def test_page_window_catalog_returns_latest_unbridged_partial_common_interval(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    current.load_scope(
        _scope("vendor-sales-by-asin", environment),
        (
            DateRange(date(2026, 8, 1), date(2026, 8, 2)),
            DateRange(date(2026, 8, 10), date(2026, 8, 12)),
        ),
    )
    current.load_scope(
        _scope("vendor-traffic-by-asin", environment),
        (DateRange(date(2026, 8, 10), date(2026, 8, 16)),),
    )
    windows = PageWindowCatalog(
        current,
        environment.packs,
        environment.contracts,
        environment.bridge,
    )

    selected = windows.resolve_default(
        PageDefaultWindowRequest(
            requirements=(
                PageWindowRequirement((("vendor_central", "GET_VENDOR_SALES_REPORT"),)),
                PageWindowRequirement((("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"),)),
            ),
            preferred_duration_days=7,
        )
    )

    assert selected is not None
    assert selected.window == DateRange(date(2026, 8, 10), date(2026, 8, 12))
    assert selected.completeness is PageDefaultWindowCompleteness.PARTIAL


def test_page_window_catalog_refuses_a_missing_required_source_scope(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    current.load_scope(
        _scope("vendor-sales-by-asin", environment),
        (DateRange(date(2026, 8, 1), date(2026, 8, 20)),),
    )
    windows = PageWindowCatalog(
        current,
        environment.packs,
        environment.contracts,
        environment.bridge,
    )

    assert (
        windows.resolve_default(
            PageDefaultWindowRequest(
                requirements=(
                    PageWindowRequirement((("vendor_central", "GET_VENDOR_SALES_REPORT"),)),
                    PageWindowRequirement((("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"),)),
                ),
                preferred_duration_days=7,
            )
        )
        is None
    )


def test_page_window_catalog_returns_exact_contributors_and_runtime_bounds(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    sales_scope = _scope("vendor-sales-by-asin", environment)
    traffic_scope = _scope("vendor-traffic-by-asin", environment)
    sales_loaded_at = NOW - timedelta(hours=2)
    traffic_loaded_at = NOW - timedelta(hours=1)
    current.load_scope(
        sales_scope,
        (DateRange(date(2026, 8, 1), date(2026, 8, 20)),),
        published_at=sales_loaded_at,
    )
    current.load_scope(
        traffic_scope,
        (
            DateRange(date(2026, 8, 8), date(2026, 8, 10)),
            DateRange(date(2026, 8, 12), date(2026, 8, 14)),
        ),
        published_at=traffic_loaded_at,
    )
    requirement = SourceCoverageRequirement(
        (
            ("vendor_central", "GET_VENDOR_SALES_REPORT"),
            ("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"),
        )
    )

    coverage = PageWindowCatalog(
        current,
        environment.packs,
        environment.contracts,
        environment.bridge,
    ).source_coverage(SourceCoverageRequest((requirement,)))

    assert tuple(
        (item.source_dataset, item.contributor_id, item.intervals, item.loaded_at)
        for item in coverage.contributors
    ) == (
        (
            "GET_VENDOR_SALES_REPORT",
            sales_scope.identity,
            (DateRange(date(2026, 8, 1), date(2026, 8, 20)),),
            sales_loaded_at,
        ),
        (
            "GET_VENDOR_TRAFFIC_REPORT",
            traffic_scope.identity,
            (
                DateRange(date(2026, 8, 8), date(2026, 8, 10)),
                DateRange(date(2026, 8, 12), date(2026, 8, 14)),
            ),
            traffic_loaded_at,
        ),
    )
    assert coverage.bounds.intervals == (
        DateRange(date(2026, 8, 8), date(2026, 8, 10)),
        DateRange(date(2026, 8, 12), date(2026, 8, 14)),
    )
    assert coverage.bounds.earliest_start == date(2026, 8, 8)
    assert coverage.bounds.latest_end == date(2026, 8, 14)
    assert coverage.latest_loaded_at == traffic_loaded_at


def test_a_writer_of_another_fact_never_empties_a_metrics_source_coverage(
    environment: PublicationEnvironment,
) -> None:
    """D2 live acceptance, 2026-10-16: campaigns baked unavailable.

    `amc-exposure-groups` writes `fct_exposure_group_outcome` from the same `dsp_impressions`
    dataset as `amc-dsp-impressions` (`fct_ad_exposure`). Undelivered, it emptied the
    intersection for every metric on that dataset. Scoped to the metric's facts, it is ignored.
    """
    writers = ("amc-dsp-impressions", "amc-exposure-groups")
    current = RecordingCurrent()
    current.load_scope(_scope("amc-dsp-impressions", environment), (DateRange(DAY_ONE, DAY_ONE),))
    packs = tuple(environment.packs_by_id[item] for item in writers)
    contracts = tuple(environment.contracts_by_id[item] for item in writers)
    catalog = PageWindowCatalog(current, packs, contracts, environment.bridge)
    sources = (("amc", "dsp_impressions"),)

    scoped = catalog.source_coverage(
        SourceCoverageRequest((SourceCoverageRequirement(sources, facts=("fct_ad_exposure",)),))
    )
    assert scoped.bounds.intervals == (DateRange(DAY_ONE, DAY_ONE),)
    assert len(scoped.contributors) == 1

    # Paired control: unscoped, the undelivered writer of the other fact empties the coverage.
    unscoped = catalog.source_coverage(SourceCoverageRequest((SourceCoverageRequirement(sources),)))
    assert unscoped.bounds.intervals == ()
    # And a scoped requirement whose fact has no writer on that source fails closed.
    absent = catalog.source_coverage(
        SourceCoverageRequest((SourceCoverageRequirement(sources, facts=("fct_unknown",)),))
    )
    assert absent.bounds.intervals == ()


def test_every_page_coverage_builder_scopes_each_requirement_to_its_metrics_facts() -> None:
    """The bake, precompute and serve builders cannot fall back to the unscoped rule."""
    root = Path(__file__).resolve().parents[2]
    catalog = load_catalog(root)
    built = (
        *campaign_coverage_requirements(load_page_schemas(root), catalog),
        *scorecard_coverage_requirements(catalog),
        *new_customers_coverage_requirements(catalog),
        *retail_health_coverage_requirements(load_v2_page_schemas(root), catalog),
    )
    assert built
    for requirement in built:
        assert requirement.facts, requirement
        assert requirement.facts == tuple(sorted(requirement.facts))
    # The campaigns spend requirement names the exposure fact, never another dsp writer's fact.
    exposure = [r for r in built if ("amc", "dsp_impressions") in r.sources]
    assert exposure and all("fct_exposure_group_outcome" not in r.facts for r in exposure)


def test_every_sourced_metric_scopes_to_a_fact_some_writer_of_each_source_targets() -> None:
    """Totality and structure over the shipped catalog.

    No served page or chat metric can raise at composition, and none can be scoped to a fact
    that no coverage writer of its source writes.
    """
    root = Path(__file__).resolve().parents[2]
    catalog = load_catalog(root)
    contracts = {contract.contract_id: contract for _, contract in load_contracts(root)}
    writers = [
        contract
        for pack in load_source_packs(root)
        if pack.role.value == "fact"
        and (contract := contracts.get(pack.contract_id)) is not None
        and contract.contributes_to_coverage
    ]
    sourced = [metric for metric in catalog.metrics if catalog.sources_for(metric)]
    assert sourced
    for metric in sourced:
        requirement = metric_coverage_requirement(catalog, metric)
        for system, dataset in requirement.sources:
            assert any(
                writer.source_system == system
                and writer.source_dataset == dataset
                and writer.target_table in requirement.facts
                for writer in writers
            ), (metric.name, system, dataset, requirement.facts)


def test_a_metric_without_a_fact_is_refused_rather_than_left_unscoped() -> None:
    class _NoFactCatalog:
        def sources_for(self, metric: object) -> tuple[tuple[str, str], ...]:
            del metric
            return (("amc", "dsp_impressions"),)

        def facts_read(self, metric: object) -> tuple[str, ...]:
            del metric
            return ()

    catalog = cast("Catalog", _NoFactCatalog())
    metric = cast("Any", type("_Metric", (), {"name": "spend"})())
    with pytest.raises(ValueError, match="no coverage source or fact"):
        metric_coverage_requirement(catalog, metric)


def test_source_coverage_requirement_refuses_a_malformed_fact_list() -> None:
    sources = (("amc", "dsp_impressions"),)
    for facts, message in (
        (["fct_ad_exposure"], "tuple of fact names"),
        (("",), "nonblank"),
        (("fct_ad_exposure", "fct_ad_exposure"), "cannot repeat"),
        (("fct_b", "fct_a"), "canonical sorted order"),
    ):
        with pytest.raises(ValueError, match=message):
            SourceCoverageRequirement(sources, facts=cast("tuple[str, ...]", facts))


def test_page_window_catalog_source_coverage_returns_empty_bounds_for_missing_publication(
    environment: PublicationEnvironment,
) -> None:
    current = RecordingCurrent()
    sales_scope = _scope("vendor-sales-by-asin", environment)
    traffic_scope = _scope("vendor-traffic-by-asin", environment)
    current.load_scope(
        sales_scope,
        (DateRange(date(2026, 8, 1), date(2026, 8, 20)),),
    )
    requirements = (
        SourceCoverageRequirement((("vendor_central", "GET_VENDOR_SALES_REPORT"),)),
        SourceCoverageRequirement((("vendor_central", "GET_VENDOR_TRAFFIC_REPORT"),)),
    )

    coverage = PageWindowCatalog(
        current,
        environment.packs,
        environment.contracts,
        environment.bridge,
    ).source_coverage(SourceCoverageRequest(requirements))

    assert tuple((item.contributor_id, item.intervals) for item in coverage.contributors) == (
        (sales_scope.identity, (DateRange(date(2026, 8, 1), date(2026, 8, 20)),)),
        (traffic_scope.identity, ()),
    )
    assert coverage.bounds.intervals == ()
    assert coverage.bounds.earliest_start is None
    assert coverage.bounds.latest_end is None
    assert coverage.latest_loaded_at == NOW - timedelta(hours=1)


def test_metric_window_default_trims_the_latest_partial_interval_to_the_preferred_days() -> None:
    """Coverage shorter than the preferred week still defaults, trailing-trimmed, never padded."""

    @dataclass(frozen=True, slots=True)
    class _StubWindowCatalog:
        intervals: tuple[DateRange, ...]

        def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
            del request
            return SourceCoverageResult(
                contributors=(),
                bounds=SourceCoverageBounds(
                    intervals=self.intervals,
                    earliest_start=self.intervals[0].start if self.intervals else None,
                    latest_end=self.intervals[-1].end if self.intervals else None,
                ),
                latest_loaded_at=None,
            )

    catalog = load_catalog(ROOT)
    metric = catalog.metric("retail_revenue_shipped")
    assert metric is not None
    short = DateRange(date(2026, 8, 10), date(2026, 8, 11))
    defaults = MetricWindowDefaults(
        cast("PageWindowCatalog", _StubWindowCatalog((short,))), catalog
    )
    assert defaults.default_window(metric) == short

    long = DateRange(date(2026, 8, 1), date(2026, 8, 20))
    defaults = MetricWindowDefaults(cast("PageWindowCatalog", _StubWindowCatalog((long,))), catalog)
    assert defaults.default_window(metric) == DateRange(date(2026, 8, 14), date(2026, 8, 20))

    empty = MetricWindowDefaults(cast("PageWindowCatalog", _StubWindowCatalog(())), catalog)
    assert empty.default_window(metric) is None


@pytest.mark.covers("ADR-070")
def test_per_pack_inspection_attests_each_pack_in_its_own_read_with_both_windows(
    environment: PublicationEnvironment,
) -> None:
    """One exact read per source pack, both comparison windows inside it, merged slots."""
    current = RecordingCurrent()
    current_window = DateRange(DAY_ONE + timedelta(days=8), DAY_ONE + timedelta(days=9))
    prior_window = DateRange(DAY_ONE, DAY_ONE + timedelta(days=1))
    request = PresentationEvidenceInspectionRequest(
        plans=(
            PresentationEvidenceInspectionPlan(
                PresentationEvidenceRole.CURRENT,
                _conversion_plan(environment, current_window),
            ),
            PresentationEvidenceInspectionPlan(
                PresentationEvidenceRole.COMPARATOR,
                _conversion_plan(environment, prior_window),
            ),
        ),
        contracts=environment.contracts,
        now=NOW,
    )

    inspected = PerPackPresentationEvidenceInspection(
        current, environment.packs, environment.bridge
    ).inspect(request)

    assert len(current.exact_calls) == 2
    both_window_days = {
        DAY_ONE,
        DAY_ONE + timedelta(days=1),
        DAY_ONE + timedelta(days=8),
        DAY_ONE + timedelta(days=9),
    }
    for read in current.exact_calls:
        assert len({partition.scope.identity for partition in read}) == 1
        assert {partition.bucket.window.start for partition in read} == both_window_days
    assert {read[0].scope.dataset.value for read in current.exact_calls} == {
        "fct_product_page_views",
        "fct_retail_sales",
    }
    strict = PublicationSelector(RecordingCurrent(), environment.packs, environment.bridge).inspect(
        request
    )
    assert inspected == strict


@pytest.mark.covers("ADR-070")
def test_verified_serving_and_per_pack_inspection_both_serve_a_14d_pair(
    environment: PublicationEnvironment,
) -> None:
    """The 14d pair binds the same evidence selection-wide and as one read per pack."""
    current_window = DateRange(DAY_ONE + timedelta(days=14), DAY_ONE + timedelta(days=27))
    prior_window = DateRange(DAY_ONE, DAY_ONE + timedelta(days=13))
    request = PresentationEvidenceInspectionRequest(
        plans=(
            PresentationEvidenceInspectionPlan(
                PresentationEvidenceRole.CURRENT,
                _conversion_plan(environment, current_window),
            ),
            PresentationEvidenceInspectionPlan(
                PresentationEvidenceRole.COMPARATOR,
                _conversion_plan(environment, prior_window),
            ),
        ),
        contracts=environment.contracts,
        now=NOW,
    )

    strict_current = RecordingCurrent()
    strict = PublicationSelector(strict_current, environment.packs, environment.bridge).inspect(
        request
    )
    assert len(strict_current.exact_calls) == 1
    assert len(strict_current.exact_calls[0]) <= MAX_EXACT_CURRENT_PARTITIONS

    current = RecordingCurrent()
    inspected = PerPackPresentationEvidenceInspection(
        current, environment.packs, environment.bridge
    ).inspect(request)

    assert len(current.exact_calls) == 2
    assert all(len(read) <= MAX_PRESET_SNAPSHOT_PARTITIONS for read in current.exact_calls)
    assert sum(len(read) for read in current.exact_calls) <= MAX_EXACT_CURRENT_PARTITIONS
    assert all(slot.present for slot in inspected.slots)
    assert inspected == strict


@pytest.mark.covers("ADR-070")
def test_per_pack_snapshot_beyond_the_transaction_bound_refuses_before_any_read(
    environment: PublicationEnvironment,
) -> None:
    """A pack is never split across independent reads: over its bound it refuses whole."""
    current = RecordingCurrent()
    days = 501
    current_window = DateRange(
        DAY_ONE + timedelta(days=days), DAY_ONE + timedelta(days=2 * days - 1)
    )
    prior_window = DateRange(DAY_ONE, DAY_ONE + timedelta(days=days - 1))

    with pytest.raises(ValidationError) as refused:
        PerPackPresentationEvidenceInspection(
            current, environment.packs, environment.bridge
        ).inspect(
            PresentationEvidenceInspectionRequest(
                plans=(
                    PresentationEvidenceInspectionPlan(
                        PresentationEvidenceRole.CURRENT,
                        _sales_plan(environment, current_window),
                    ),
                    PresentationEvidenceInspectionPlan(
                        PresentationEvidenceRole.COMPARATOR,
                        _sales_plan(environment, prior_window),
                    ),
                ),
                contracts=environment.contracts,
                now=NOW,
            )
        )

    assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0003"
    assert refused.value.context["required_partitions"] == 1_002
    assert current.exact_calls == []
    # ADR-142: the pack's figures, and not its scope, reach the page's refusal.
    assert dict(classify_refusal(refused.value).refusal_context) == {
        "maximum_partitions": 1_000,
        "required_partitions": 1_002,
    }


@pytest.mark.covers("ADR-070")
def test_per_pack_inspection_keeps_stable_absence_per_role_slot(
    environment: PublicationEnvironment,
) -> None:
    """A pack absent in its own read absents only the plans that need it, like the strict path."""
    current = RecordingCurrent(exact_absent_datasets=frozenset({"fct_product_page_views"}))

    inspected = PerPackPresentationEvidenceInspection(
        current, environment.packs, environment.bridge
    ).inspect(
        PresentationEvidenceInspectionRequest(
            plans=(
                PresentationEvidenceInspectionPlan(
                    PresentationEvidenceRole.CURRENT,
                    _conversion_plan(environment, DateRange(DAY_ONE, DAY_ONE)),
                ),
                PresentationEvidenceInspectionPlan(
                    PresentationEvidenceRole.COMPARATOR,
                    _sales_plan(environment, DateRange(DAY_ONE, DAY_ONE)),
                ),
            ),
            contracts=environment.contracts,
            now=NOW,
        )
    )

    assert not inspected.slots[0].present
    assert inspected.slots[1].present


# --- Plan 4.3 (DB21, S5): select_exact and the pinned bake source reads -----------------------

from adapters.outbound.bake_sources import (  # noqa: E402
    BakeQueryCapture,
    BakeRowHorizon,
    BakeSourceReads,
    decode_reads,
    encode_reads,
    reads_digest,
    task_source,
)
from adapters.outbound.publication_selection import publication_digest  # noqa: E402
from domain.operations.execution_graph_release import (  # noqa: E402
    DurableBakeSourceManifestInvalidError,
)
from domain.shared_kernel.errors import InvariantError  # noqa: E402
from ports.outbound.publication_selection import (  # noqa: E402
    PinnedPublication,
    PublicationSelectionExactRequest,
)
from ports.outbound.query import QueryRequest  # noqa: E402


@dataclass
class _ImmutableCurrent(RecordingCurrent):
    """CURRENT plus the immutable records `immutable_exact` resolves by id."""

    advanced: bool = False
    """After the pin, CURRENT moves on; the immutable records stay."""
    immutable_calls: int = 0

    def immutable_exact(
        self, pins: tuple[tuple[str, str, int], ...]
    ) -> tuple[SuccessfulPublication, ...]:
        self.immutable_calls += 1
        by_id = {
            item.publication_id.value: item
            for items in self.scope_publications.values()
            for item in items
        }
        return tuple(by_id[publication] for _partition, publication, _token in pins)

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon
        found = super().current_for_scope(scope)
        return found[:-1] if self.advanced else found


def _loaded(environment: PublicationEnvironment) -> tuple[_ImmutableCurrent, PublicationScope]:
    current = _ImmutableCurrent()
    scope = _scope("vendor-sales-by-asin", environment)
    current.load_scope(scope, (DateRange(date(2026, 8, 1), date(2026, 8, 3)),))
    return current, scope


def test_select_exact_resolves_the_pinned_records_and_never_reads_current(
    environment: PublicationEnvironment,
) -> None:
    current, scope = _loaded(environment)
    published = current.scope_publications[scope.identity]
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    pins = tuple(
        PinnedPublication(
            item.partition.identity,
            item.publication_id.value,
            item.fencing_token,
            publication_digest(item),
        )
        for item in published
    )

    resolved = selector.select_exact(PublicationSelectionExactRequest(pins))

    assert resolved == published
    assert (current.scope_calls, current.exact_calls) == ([], [])
    # Control: one pin whose digest is not the stored record's is refused.
    wrong = (replace(pins[0], digest="0" * 64), *pins[1:])
    with pytest.raises(InvariantError):
        selector.select_exact(PublicationSelectionExactRequest(wrong))


_ROW = BakeRowHorizon(date(2026, 10, 14), 123)
"""A fictional row horizon: every recorded whole-scope read declares it (A5.1 I9)."""


def test_a_task_replays_its_pinned_reads_with_zero_current_reads_after_current_advances(
    environment: PublicationEnvironment,
) -> None:
    current, scope = _loaded(environment)
    selector = PublicationSelector(current, environment.packs, environment.bridge)
    reads = BakeSourceReads(current, selector)
    with reads.snapshot(), reads.recording(_ROW) as handle:
        pinned_view = reads.current_for_scope(scope, _ROW.window(1))
    recorded = handle.reads
    assert reads.current_reads == 1
    current.advanced = True

    with reads.replaying(decode_reads(encode_reads(recorded), reads_digest(recorded)), _ROW):
        assert reads.current_for_scope(scope, _ROW.window(1)) == pinned_view
        assert reads.current_reads == 1
        assert reads.select_exact_calls == 1
    # Control: outside replay the same read reaches the advanced CURRENT.
    assert reads.current_for_scope(scope, _ROW.window(1)) != pinned_view


def test_an_unpinned_read_in_replay_is_the_fatal_manifest_refusal_and_a_repeat_is_free(
    environment: PublicationEnvironment,
) -> None:
    """A5.1 I2: keyed replay answers a repeated read equal; an unpinned key is fatal."""
    current, scope = _loaded(environment)
    reads = BakeSourceReads(
        current, PublicationSelector(current, environment.packs, environment.bridge)
    )
    with reads.snapshot(), reads.recording(_ROW) as handle:
        reads.current_for_scope(scope, _ROW.window(1))
    recorded = handle.reads
    with reads.replaying(recorded, _ROW):
        first = reads.current_for_scope(scope, _ROW.window(1))
        assert reads.current_for_scope(scope, _ROW.window(1)) == first
        with pytest.raises(DurableBakeSourceManifestInvalidError):
            reads.current_for_scope(scope, _ROW.window(2))
    with pytest.raises(DurableBakeSourceManifestInvalidError):
        decode_reads(encode_reads(recorded), "sha256:" + "0" * 64)


@dataclass
class _NoQueries:
    calls: int = 0

    def run(self, request: QueryRequest) -> Any:
        del request
        self.calls += 1
        raise AssertionError("the bind pass must never execute a query")


def test_the_bind_pass_captures_complete_requests_with_zero_executions(
    environment: PublicationEnvironment,
) -> None:
    """Identical manifests bind identical requests; a changed request changes the digest."""
    current, scope = _loaded(environment)
    selection = PublicationSelector(current, environment.packs, environment.bridge).select(
        PublicationSelectionRequest(
            (_sales_plan(environment, DateRange(date(2026, 8, 1), date(2026, 8, 3))),),
            environment.contracts,
            NOW,
        )
    )
    request = QueryRequest(plan=selection.plans[0], max_rows=10)
    inner = _NoQueries()
    capture = BakeQueryCapture(inner)
    with capture.capturing_scope() as handle:
        assert capture.run(request).rows == ()
        assert capture.run(request).rows == ()
    captured = handle.requests

    first = task_source("campaigns", "7d", (), captured)
    again = task_source("campaigns", "7d", (), captured)
    changed = task_source("campaigns", "7d", (), (replace(request, max_rows=9),))

    assert inner.calls == capture.executions == 0
    assert first.plan_count == 1
    assert first.digest == again.digest
    assert changed.digest != first.digest
    del scope


# --- Pack 4: the publication-gated DSP reach writer (plan D6, ADR-109) --------------------------

_DSP = {"marketplace_key": "amazon_us", "channel_key": "amazon_dsp", "ad_product_key": "amazon_dsp"}


def _exposure_plan(
    environment: PublicationEnvironment, metric_name: str, filters: dict[str, str]
) -> QueryPlan:
    day = DateRange(DAY_ONE, DAY_ONE)
    return plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name=metric_name,
            grains=("channel", "campaign", "ad_product"),
            window=day,
            filters=filters,
            pack_coverage=(PackDayVersions("amc-dsp-reach", {DAY_ONE: 1}),),
        ),
        FactShape.for_fact("fct_ad_exposure", environment.contracts),
    )


def _bound_contracts(environment: PublicationEnvironment, query: QueryPlan) -> set[str]:
    current = RecordingCurrent()
    PublicationSelector(current, environment.packs, environment.bridge).select(
        PublicationSelectionRequest((query,), environment.contracts, NOW)
    )
    (requested,) = current.exact_calls
    scopes = {item.scope for item in requested}
    return {
        pack.contract_id
        for pack in environment.packs
        if pack.dataset.value == query.fact
        and set(
            publication_scopes_for(
                PublicationScopeRequest(
                    pack=pack,
                    contract=environment.contracts_by_id[pack.contract_id],
                    bridge=environment.bridge,
                    filters={"marketplace_key": "amazon_us"},
                )
            )
        )
        & scopes
    }


def test_dsp_reach_binds_its_writer_only_when_the_request_pins_dsp_rows(
    environment: PublicationEnvironment,
) -> None:
    pinned = _bound_contracts(environment, _exposure_plan(environment, "reach_users", _DSP))
    assert pinned == {"amc-reach-frequency", "amc-dsp-reach"}
    # Paired control: unpinned reach binds exactly what it bound before pack 4.
    unpinned = _bound_contracts(
        environment, _exposure_plan(environment, "reach_users", {"marketplace_key": "amazon_us"})
    )
    assert unpinned == {"amc-reach-frequency"}


def test_dsp_average_frequency_binds_the_impressions_and_reach_writers(
    environment: PublicationEnvironment,
) -> None:
    """Impressions per DSP shopper reads both DSP writers' rows, never clicks or views."""
    pinned = _bound_contracts(environment, _exposure_plan(environment, "avg_frequency", _DSP))
    assert pinned == {"amc-reach-frequency", "amc-dsp-impressions", "amc-dsp-reach"}
    # Paired control: unpinned frequency doesn't bind the gated reach writer.
    unpinned = _bound_contracts(
        environment,
        _exposure_plan(environment, "avg_frequency", {"marketplace_key": "amazon_us"}),
    )
    assert unpinned == {"amc-reach-frequency", "amc-dsp-impressions"}


def test_dsp_spend_never_binds_the_reach_writer(environment: PublicationEnvironment) -> None:
    """A refused reach delivery can't block DSP spend: spend binds only the writers of spend."""
    assert _bound_contracts(environment, _exposure_plan(environment, "ad_spend", _DSP)) == {
        "amc-dsp-impressions",
        "amc-reach-frequency",
    }


def test_the_reach_writer_never_narrows_exposure_coverage(
    environment: PublicationEnvironment,
) -> None:
    """Four writers published and no DSP reach: the fact is still covered (plan D6)."""
    contributor_ids = (
        "amc-reach-frequency",
        "amc-dsp-clicks",
        "amc-dsp-impressions",
        "amc-dsp-views",
        "amc-dsp-reach",
    )
    current = RecordingCurrent()
    for contract_id in contributor_ids[:-1]:
        current.load_scope(_scope(contract_id, environment), (DateRange(DAY_ONE, DAY_ONE),))
    selected_packs = tuple(environment.packs_by_id[item] for item in contributor_ids)
    selected_contracts = tuple(environment.contracts_by_id[item] for item in contributor_ids)

    coverage = PublicationCatalog(
        current, selected_packs, selected_contracts, environment.bridge
    ).coverage_for("fct_ad_exposure", NOW)

    assert coverage is not None
    assert coverage.freshness is FreshnessStatus.FRESH
    assert coverage.intervals == (DateRange(DAY_ONE, DAY_ONE),)
    # Control: a missing writer that does contribute still fails toward never loaded.
    del current.scope_publications[_scope("amc-dsp-views", environment).identity]
    narrowed = PublicationCatalog(
        current, selected_packs, selected_contracts, environment.bridge
    ).coverage_for("fct_ad_exposure", NOW)
    assert narrowed is not None
    assert narrowed.freshness is FreshnessStatus.NEVER_LOADED


# --- Campaign delivery: DSP media quality binds only an Amazon DSP selection ------------------

from collections import Counter  # noqa: E402

from campaign_portfolio_support import campaign_display_signature  # noqa: E402
from fixture_artifacts_support import fixture_catalog, render_campaign_artifacts  # noqa: E402

from adapters.campaign_measurement import (  # noqa: E402
    CampaignArtifactMeasurementService,
    CampaignDeliveryMeasurementBatch,
    CampaignDetailMeasurementInput,
    CampaignMeasurementDependencies,
    CampaignMeasurementSelection,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope  # noqa: E402
from domain.semantics.identity import (  # noqa: E402
    CampaignKey,
    ChannelKey,
    PlatformKey,
)

_DSP_QUALITY = (
    "measurable_impressions",
    "viewable_impressions",
    "viewable_impressions_estimated",
    "viewable_rate",
)
_DETAIL_PERIOD = DateRange(DAY_ONE, DAY_ONE + timedelta(days=6))
_DETAIL_PRIOR = DateRange(DAY_ONE - timedelta(days=7), DAY_ONE - timedelta(days=1))


def _delivery_batch(
    environment: PublicationEnvironment, channel: str
) -> CampaignDeliveryMeasurementBatch:
    scope = environment.bridge.commerce_scope
    service = CampaignArtifactMeasurementService(
        CampaignMeasurementDependencies(
            scope=AuthorizedAnalyticalScope(frozenset({scope})),
            catalog=environment.metric_catalog,
            contracts=environment.contracts,
            clock=lambda: NOW,
        )
    )
    return service.delivery_batch(
        CampaignDetailMeasurementInput(
            CampaignMeasurementSelection(
                CampaignKey("campaign:fictional", scope, PlatformKey("amazon_ads")),
                ChannelKey(channel),
                MarketplaceKey("amazon_us"),
                campaign_display_signature("Fictional Campaign"),
            ),
            _DETAIL_PERIOD,
            _DETAIL_PRIOR,
        )
    )


def _inspect_delivery(
    environment: PublicationEnvironment, batch: CampaignDeliveryMeasurementBatch
) -> tuple[bool, ...]:
    """Inspect a delivery batch through the real selector; one flag per plan, bound or not."""
    inspection = PublicationSelector(
        RecordingCurrent(), environment.packs, environment.bridge
    ).inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in batch.prepared.plans
            ),
            contracts=batch.prepared.contracts,
            now=batch.prepared.now,
        )
    )
    return tuple(slot.bound is not None for slot in inspection.slots)


@pytest.mark.parametrize("channel", ["amazon_sponsored_products", "amazon_sponsored_brands"])
def test_a_sponsored_campaign_delivery_binds_every_plan_and_requests_no_dsp_quality(
    environment: PublicationEnvironment, channel: str
) -> None:
    """A sponsored campaign's delivery batch binds every plan through the real selector.

    campaign-detail-500 cause A: a sponsored pin binds no DSP views pack (019d066c), so the
    batch must not request DSP media quality.
    """
    batch = _delivery_batch(environment, channel)

    assert _inspect_delivery(environment, batch) == (True,) * 20
    assert tuple(role for slot, role, _ in batch.slots if slot is None) == ()
    assert len(batch.prepared.plans) == len(batch.slots) == 20


def test_an_amazon_dsp_campaign_delivery_still_binds_its_dsp_quality_operands(
    environment: PublicationEnvironment,
) -> None:
    """Paired control: a DSP pin keeps all four media-quality operands, each bound."""
    batch = _delivery_batch(environment, "amazon_dsp")

    assert tuple(role for slot, role, _ in batch.slots if slot is None) == _DSP_QUALITY
    assert tuple(plan.logical.metric.name for plan in batch.prepared.plans[-4:]) == _DSP_QUALITY
    assert len(batch.prepared.plans) == len(batch.slots) == 24
    assert _inspect_delivery(environment, batch) == (True,) * 24


def test_the_bake_renders_a_sponsored_delivery_through_the_real_selector(
    environment: PublicationEnvironment,
) -> None:
    """The insight-precompute and preset renderer reach the same binding as campaign detail.

    Every fixture campaign, sponsored and DSP, renders a 7D window through the real
    `PublicationSelector`: a sponsored delivery is qualified with DSP media quality typed absent
    for its channel, and a DSP delivery keeps its measured media quality.
    """
    anchor = fixture_catalog().shape.event_anchor
    current = DateRange(anchor - timedelta(days=6), anchor)
    prior = DateRange(current.start - timedelta(days=7), current.start - timedelta(days=1))

    rendered = render_campaign_artifacts(
        current,
        prior,
        channel=None,
        evidence=PublicationSelector(RecordingCurrent(), environment.packs, environment.bridge),
    )

    assert rendered is not None
    served = Counter(
        (
            None if detail is None else detail.selection.channel,
            None
            if delivery is None
            else (
                delivery.outcome,
                delivery.dsp_quality.outcome,
                delivery.dsp_quality.reason_code,
            ),
        )
        for detail, delivery in zip(rendered.details, rendered.deliveries, strict=True)
    )
    not_measured = ("qualified", "unavailable", "dsp_quality_not_measured_for_channel")
    # Two Sponsored Products campaigns reduce no attribution detail in this window (the binding
    # stub gives the same split), so neither carries a detail pair.
    assert served == {
        ("Sponsored Products", not_measured): 27,
        ("Sponsored Brands", not_measured): 1,
        ("Amazon DSP", ("complete", "available", None)): 30,
        (None, None): 2,
    }


# --- dsp-cpc-derive: the bake derives cost per click from the accepted spend and clicks -------

from decimal import Decimal  # noqa: E402

from fixture_artifacts_support import FixtureQueries  # noqa: E402

from domain.measurement.planning import measure_alias  # noqa: E402
from domain.presentation.campaign_attribution import CampaignDeliverySlot  # noqa: E402
from ports.outbound.query import QueryResult, QueryRow  # noqa: E402


@dataclass
class _SplitCpcQueries(FixtureQueries):
    """Return each `cpc` group as dev's Athena does over Amazon DSP's two writers (2026-10-19).

    The DSP clicks writer's row has no currency and no spend; the DSP impressions writer's row
    has the currency and spend but no clicks.
    """

    def run(self, request: QueryRequest) -> QueryResult:
        result = super().run(request)
        if request.plan.logical.metric.name != "cpc":
            return result
        rows: list[QueryRow] = []
        for row in result.rows:
            rows.extend(
                (
                    {**row, "currency_code": None, measure_alias(0): None},
                    {**row, measure_alias(1): None},
                )
            )
        return replace(result, rows=tuple(rows))


def _cpc(spend: object, clicks: object) -> Decimal | int:
    """Divide spend by clicks at the planner's four places; a day with no clicks is zero."""
    if not clicks:
        return 0
    return (
        Decimal(cast("Decimal | int", spend)) / Decimal(cast("Decimal | int", clicks))
    ).quantize(Decimal("0.0001"))


def test_the_bake_derives_dsp_cpc_from_its_split_spend_and_clicks_writers(
    environment: PublicationEnvironment,
) -> None:
    """`reduce_delivery` divides the accepted spend and clicks, so the split never reaches it.

    Before the fix the bake planned `cpc`, got the split pair, refused it, and rendered no DSP
    detail at all, so every DSP detail fell back to request time.
    """
    anchor = fixture_catalog().shape.event_anchor
    current = DateRange(anchor - timedelta(days=6), anchor)
    prior = DateRange(current.start - timedelta(days=7), current.start - timedelta(days=1))
    queries = _SplitCpcQueries()

    rendered = render_campaign_artifacts(
        current,
        prior,
        evidence=PublicationSelector(RecordingCurrent(), environment.packs, environment.bridge),
        queries=queries,
    )

    assert rendered is not None
    assert len(rendered.deliveries) == 30
    for delivery in rendered.deliveries:
        assert delivery is not None
        assert delivery.outcome == "complete"
        for values in (delivery.summary, delivery.comparison):
            by_slot = {item.slot: item.value for item in values}
            spend = by_slot[CampaignDeliverySlot.AD_SPEND]
            clicks = by_slot[CampaignDeliverySlot.CLICKS]
            cpc = by_slot[CampaignDeliverySlot.CPC]
            assert spend is not None and clicks is not None and cpc is not None
            assert cpc == replace(spend, value=_cpc(spend.value, clicks.value))
        for row in delivery.exact_rows:
            for role in ("current", "prior"):
                day = cast("dict[str, object]", row[role])
                assert day["cpc"] == _cpc(day["ad_spend"], day["clicks"])
    assert "cpc" not in {call.plan.logical.metric.name for call in queries.calls}


# --- A customer's media pin no writer serves is a classified refusal (campaign-detail-500) ------

_SPONSORED_PIN_REFUSALS = (
    (
        "viewable_impressions",
        "amazon_sponsored_products",
        "Viewable impressions",
        "Sponsored Products",
    ),
    (
        "measurable_impressions",
        "amazon_sponsored_brands",
        "Measurable impressions",
        "Sponsored Brands",
    ),
    (
        "viewable_impressions_estimated",
        "amazon_sponsored_products",
        "Viewable impressions (estimated)",
        "Sponsored Products",
    ),
    ("viewable_rate", "amazon_sponsored_brands", "Viewable rate", "Sponsored Brands"),
)


def _pinned_exposure_plan(
    environment: PublicationEnvironment, metric_name: str, channel: str
) -> QueryPlan:
    return _media_pinned_plan(environment, metric_name, {"channel_key": channel})


def _media_pinned_plan(
    environment: PublicationEnvironment, metric_name: str, pins: dict[str, str]
) -> QueryPlan:
    return plan(
        environment.metric_catalog,
        PlanRequest(
            metric_name=metric_name,
            grains=("channel",),
            window=DateRange(DAY_ONE, DAY_ONE),
            filters={"marketplace_key": "amazon_us", **pins},
        ),
        FactShape.for_fact("fct_ad_exposure", environment.contracts),
    )


@pytest.mark.parametrize("case", _SPONSORED_PIN_REFUSALS)
def test_a_dsp_only_measure_pinned_to_a_sponsored_channel_is_a_classified_refusal(
    environment: PublicationEnvironment, case: tuple[str, str, str, str]
) -> None:
    """Chat's DSP-views measure under a sponsored pin refuses in business words, before any read.

    campaign-detail-500 review finding 1: `amc-dsp-views` stamps `channel_key = amazon_dsp`, so a
    sponsored pin binds no pack (019d066c) and was the built-in `ValueError` chat reported as
    `UNEXPECTED_FAILURE`.
    """
    metric_name, channel, label, requested = case
    current = RecordingCurrent()
    selector = PublicationSelector(
        current, environment.packs, environment.bridge, taxonomy=load_media_taxonomy(ROOT)
    )
    query = _pinned_exposure_plan(environment, metric_name, channel)

    with pytest.raises(DomainError) as refused:
        selector.select(PublicationSelectionRequest((query,), environment.contracts, NOW))

    assert type(refused.value) is DomainError
    assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
    assert refused.value.user_visible_description == (
        f"The {label} metric is measured only for Amazon DSP, not {requested}. "
        "Ask about Amazon DSP, or ask without a channel."
    )
    assert refused.value.context == {
        "fact": "fct_ad_exposure",
        "requested": {"channel_key": channel},
        "measured_for": {"channel_key": ["amazon_dsp"]},
    }
    assert not refused.value.alarms
    assert current.exact_calls == []
    # Paired control: the same measure pinned to Amazon DSP binds.
    bound = selector.select(
        PublicationSelectionRequest(
            (_pinned_exposure_plan(environment, metric_name, "amazon_dsp"),),
            environment.contracts,
            NOW,
        )
    )
    assert len(bound.plans) == 1


def test_a_server_built_pin_conflict_stays_an_invariant(
    environment: PublicationEnvironment,
) -> None:
    """A selector given no taxonomy (pages, bakes) chose its own pins, so the conflict is a bug."""
    selector = PublicationSelector(RecordingCurrent(), environment.packs, environment.bridge)
    query = _pinned_exposure_plan(environment, "viewable_impressions", "amazon_sponsored_products")

    with pytest.raises(
        ValueError, match=r"^fct_ad_exposure: no source pack matches \('dsp_views',\)\.$"
    ):
        selector.select(PublicationSelectionRequest((query,), environment.contracts, NOW))


def test_a_measure_with_no_writer_at_all_stays_an_invariant(
    environment: PublicationEnvironment,
) -> None:
    """No contract writes the measure, so there's no pin to blame: the bare no-pack defect."""
    selector = PublicationSelector(
        RecordingCurrent(),
        environment.packs,
        environment.bridge,
        taxonomy=load_media_taxonomy(ROOT),
    )
    query = _pinned_exposure_plan(environment, "viewable_impressions", "amazon_sponsored_products")
    without_views = tuple(
        contract for contract in environment.contracts if contract.contract_id != "amc-dsp-views"
    )

    with pytest.raises(
        ValueError, match=r"^fct_ad_exposure: no source pack matches \('dsp_views',\)\.$"
    ):
        selector.select(PublicationSelectionRequest((query,), without_views, NOW))


# Each media column `amc-dsp-views` stamps, alone and together (campaign-detail-500 review 2).
# `viewable_impressions` lists every media grain as valid and chat passes every entity kind the
# customer names, so a question naming Sponsored Products as both channel and ad product pins two.
_MEDIA_PIN_REFUSALS = (
    pytest.param(
        (
            "viewable_impressions",
            {"channel_group_key": "search"},
            "The Viewable impressions metric is measured only for Display, not Search. "
            "Ask about Display, or ask without a channel group.",
            {"channel_group_key": ["display"]},
        ),
        id="channel-group",
    ),
    pytest.param(
        (
            "measurable_impressions",
            {"ad_product_key": "sponsored_products"},
            "The Measurable impressions metric is measured only for Amazon DSP, not Sponsored "
            "Products. Ask about Amazon DSP, or ask without an ad product.",
            {"ad_product_key": ["amazon_dsp"]},
        ),
        id="ad-product",
    ),
    pytest.param(
        (
            "viewable_impressions",
            {"channel_key": "amazon_sponsored_products", "ad_product_key": "sponsored_products"},
            "The Viewable impressions metric is measured only for Amazon DSP, not Sponsored "
            "Products. Ask about Amazon DSP, or ask without a channel and an ad product.",
            {"channel_key": ["amazon_dsp"], "ad_product_key": ["amazon_dsp"]},
        ),
        id="channel-and-ad-product",
    ),
    pytest.param(
        (
            "viewable_rate",
            {"channel_group_key": "search", "ad_product_key": "sponsored_products"},
            "The Viewable rate metric is measured only for Display and Amazon DSP, not Search and "
            "Sponsored Products. Ask about Display and Amazon DSP, or ask without a channel group "
            "and an ad product.",
            {"channel_group_key": ["display"], "ad_product_key": ["amazon_dsp"]},
        ),
        id="channel-group-and-ad-product",
    ),
    pytest.param(
        (
            "viewable_impressions_estimated",
            {
                "channel_group_key": "search",
                "channel_key": "amazon_sponsored_brands",
                "ad_product_key": "sponsored_brands",
            },
            "The Viewable impressions (estimated) metric is measured only for Display and "
            "Amazon DSP, not Search and Sponsored Brands. Ask about Display and Amazon DSP, or "
            "ask without a channel group, a channel and an ad product.",
            {
                "channel_group_key": ["display"],
                "channel_key": ["amazon_dsp"],
                "ad_product_key": ["amazon_dsp"],
            },
        ),
        id="every-stamped-column",
    ),
)


@pytest.mark.parametrize("case", _MEDIA_PIN_REFUSALS)
def test_every_media_pin_the_writer_contradicts_is_a_classified_refusal(
    environment: PublicationEnvironment,
    case: tuple[str, dict[str, str], str, dict[str, list[str]]],
) -> None:
    """One media pin or several, a customer's pin no writer serves refuses before any read."""
    metric_name, pins, reason, measured_for = case
    current = RecordingCurrent()
    selector = PublicationSelector(
        current, environment.packs, environment.bridge, taxonomy=load_media_taxonomy(ROOT)
    )

    with pytest.raises(DomainError) as refused:
        selector.select(
            PublicationSelectionRequest(
                (_media_pinned_plan(environment, metric_name, pins),), environment.contracts, NOW
            )
        )

    assert type(refused.value) is DomainError
    assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
    assert refused.value.user_visible_description == reason
    assert refused.value.context == {
        "fact": "fct_ad_exposure",
        "requested": pins,
        "measured_for": measured_for,
    }
    assert not refused.value.alarms
    assert current.exact_calls == []


def test_a_pinned_value_the_taxonomy_cannot_name_stays_an_invariant(
    environment: PublicationEnvironment,
) -> None:
    """No governed name for the pin means no grounded sentence: the bare no-pack defect."""
    selector = PublicationSelector(
        RecordingCurrent(),
        environment.packs,
        environment.bridge,
        taxonomy=load_media_taxonomy(ROOT),
    )
    query = _pinned_exposure_plan(environment, "viewable_impressions", "amazon_unnamed_channel")

    with pytest.raises(
        ValueError, match=r"^fct_ad_exposure: no source pack matches \('dsp_views',\)\.$"
    ):
        selector.select(PublicationSelectionRequest((query,), environment.contracts, NOW))


# No committed contract stamps `ecosystem_key` or `platform_key` as a column literal, so these
# two `_MEDIA_PINS` columns are exercised on a views writer that stamps one.
def _stamping(contract: DataContract, column: str, literal: str) -> DataContract:
    return replace(
        contract,
        fields=(
            *(mapping for mapping in contract.fields if mapping.target_column != column),
            FieldMapping(target_column=column, target_type="string", literal_value=literal),
        ),
    )


def _views_side(filters: dict[str, str]) -> selection_module._FactSide:  # pyright: ignore[reportPrivateUsage]
    return selection_module._FactSide(  # pyright: ignore[reportPrivateUsage]
        "fct_ad_exposure",
        ("dsp_views",),
        {"marketplace_key": "amazon_us", **filters},
        DateRange(DAY_ONE, DAY_ONE),
        ("viewable_impressions",),
        "Viewable impressions",
    )


def _views_contract(environment: PublicationEnvironment) -> DataContract:
    return next(c for c in environment.contracts if c.contract_id == "amc-dsp-views")


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (
                "ecosystem_key",
                "amazon",
                "walmart",
                "The Viewable impressions metric is measured only for Amazon, not Walmart. "
                "Ask about Amazon, or ask without an ecosystem.",
            ),
            id="ecosystem",
        ),
        pytest.param(
            (
                "platform_key",
                "amazon_ads",
                "amazon_retail",
                "The Viewable impressions metric is measured only for Amazon Ads, not Amazon "
                "Retail. Ask about Amazon Ads, or ask without a platform.",
            ),
            id="platform",
        ),
    ],
)
def test_an_ecosystem_or_platform_pin_a_writer_stamps_otherwise_is_refused(
    environment: PublicationEnvironment, case: tuple[str, str, str, str]
) -> None:
    """The remaining two media columns name their own noun and governed values."""
    column, stamped, pinned, reason = case
    base = load_media_taxonomy(ROOT)
    taxonomy = replace(
        base,
        terms=(
            *base.terms,
            MediaTaxonomyTerm(EcosystemKey("walmart"), "Walmart", EcosystemKey("walmart")),
        ),
    )
    writer = _stamping(_views_contract(environment), column, stamped)

    refusal = selection_module._pin_refusal(  # pyright: ignore[reportPrivateUsage]
        _views_side({column: pinned}), (writer,), taxonomy
    )

    assert refusal is not None
    assert str(refusal.code) == "ADLY-PUBLICATION_SELECTION-0004"
    assert refusal.user_visible_description == reason
    assert refusal.context == {
        "fact": "fct_ad_exposure",
        "requested": {column: pinned},
        "measured_for": {column: [stamped]},
    }


def test_writers_contradicting_different_columns_stay_an_invariant(
    environment: PublicationEnvironment,
) -> None:
    """No single "measured only for" holds for both writers, so there is no grounded sentence."""
    views = _views_contract(environment)
    channel_only = replace(
        views,
        fields=tuple(m for m in views.fields if m.target_column != "ad_product_key"),
    )
    ad_product_only = replace(
        views,
        fields=tuple(m for m in views.fields if m.target_column != "channel_key"),
    )
    side = _views_side(
        {"channel_key": "amazon_sponsored_products", "ad_product_key": "sponsored_products"}
    )

    assert (
        selection_module._pin_refusal(  # pyright: ignore[reportPrivateUsage]
            side, (channel_only, ad_product_only), load_media_taxonomy(ROOT)
        )
        is None
    )


# "Measured only for" is a fact about the measure's writers, not the packs serving one window
# (campaign-detail-500 review 3 finding 1). Every committed `fct_ad_exposure` pack is daily, so a
# weekly views writer is synthesized to put a writer outside a one-day window.
_PERIODIC_VIEWS = "unit-dsp-views-weekly"
_VIEWS_NO_PACK = "unit-dsp-views-unregistered"


def _views_pack_as(
    environment: PublicationEnvironment, contract_id: str, *, weekly: bool
) -> SourcePack:
    pack = environment.packs_by_id["amc-dsp-views"]
    if not weekly:
        return replace(pack, contract_id=contract_id)
    return replace(
        pack,
        contract_id=contract_id,
        time=replace(
            pack.time,
            time_class=FactTimeClass.PERIODIC,
            grain=TimeGrain.WEEK,
            period_grains=(TimeGrain.WEEK,),
        ),
    )


def _views_unstamped(environment: PublicationEnvironment, contract_id: str) -> DataContract:
    """Return a views writer with no `channel_key` literal: it would serve any channel pin."""
    views = _views_contract(environment)
    return replace(
        views,
        contract_id=contract_id,
        fields=tuple(m for m in views.fields if m.target_column != "channel_key"),
    )


def _chat_selector(
    environment: PublicationEnvironment, current: RecordingCurrent, packs: tuple[SourcePack, ...]
) -> PublicationSelector:
    return PublicationSelector(
        current, packs, environment.bridge, taxonomy=load_media_taxonomy(ROOT)
    )


def _sponsored_views(
    environment: PublicationEnvironment, contracts: tuple[DataContract, ...]
) -> PublicationSelectionRequest:
    query = _pinned_exposure_plan(environment, "viewable_impressions", "amazon_sponsored_products")
    return PublicationSelectionRequest((query,), contracts, NOW)


def test_the_refusal_names_what_every_writer_measures_whatever_the_window(
    environment: PublicationEnvironment,
) -> None:
    """A writer outside the one-day window still counts toward what the measure is measured for.

    The weekly writer stamps Sponsored Brands, so the true scope is Amazon DSP or Sponsored
    Brands, never Amazon DSP alone, which is all the packs serving this window would say.
    """
    weekly = replace(
        _stamping(_views_contract(environment), "channel_key", "amazon_sponsored_brands"),
        contract_id=_PERIODIC_VIEWS,
    )
    current = RecordingCurrent()

    selector = _chat_selector(
        environment,
        current,
        (*environment.packs, _views_pack_as(environment, _PERIODIC_VIEWS, weekly=True)),
    )

    with pytest.raises(DomainError) as refused:
        selector.select(_sponsored_views(environment, (*environment.contracts, weekly)))

    assert type(refused.value) is DomainError
    assert str(refused.value.code) == "ADLY-PUBLICATION_SELECTION-0004"
    assert refused.value.user_visible_description == (
        "The Viewable impressions metric is measured only for Amazon DSP or Sponsored Brands, "
        "not Sponsored Products. Ask about Amazon DSP or Sponsored Brands, or ask without a "
        "channel."
    )
    assert refused.value.context == {
        "fact": "fct_ad_exposure",
        "requested": {"channel_key": "amazon_sponsored_products"},
        "measured_for": {"channel_key": ["amazon_dsp", "amazon_sponsored_brands"]},
    }
    assert not refused.value.alarms
    assert current.exact_calls == []


def test_a_writer_excluded_only_by_the_window_stays_an_invariant(
    environment: PublicationEnvironment,
) -> None:
    """A weekly writer that would serve the pin, outside a one-day window: no scope to blame."""
    current = RecordingCurrent()
    selector = _chat_selector(
        environment,
        current,
        (*environment.packs, _views_pack_as(environment, _PERIODIC_VIEWS, weekly=True)),
    )
    request = _sponsored_views(
        environment, (*environment.contracts, _views_unstamped(environment, _PERIODIC_VIEWS))
    )

    with pytest.raises(
        ValueError, match=r"^fct_ad_exposure: no source pack matches \('dsp_views',\)\.$"
    ) as raised:
        selector.select(request)

    assert type(raised.value) is ValueError
    assert current.exact_calls == []


@pytest.mark.parametrize("miss", ["registry", "contract"])
def test_a_writer_the_registry_and_contracts_disagree_on_stays_an_invariant(
    environment: PublicationEnvironment, miss: str
) -> None:
    """A writer whose literals can't be read with its pack is not evidence of what is measured.

    `registry`: a contract that would serve the pin has no source pack. `contract`: a source pack
    of the fact has no contract in the caller's map, so its literals are unknown.
    """
    if miss == "registry":
        packs = environment.packs
        contracts = (*environment.contracts, _views_unstamped(environment, _VIEWS_NO_PACK))
    else:
        packs = (*environment.packs, _views_pack_as(environment, _VIEWS_NO_PACK, weekly=False))
        contracts = environment.contracts
    current = RecordingCurrent()

    with pytest.raises(
        ValueError, match=r"^fct_ad_exposure: no source pack matches \('dsp_views',\)\.$"
    ) as raised:
        _chat_selector(environment, current, packs).select(_sponsored_views(environment, contracts))

    assert type(raised.value) is ValueError
    assert current.exact_calls == []
