# SPDX-License-Identifier: MIT-0
"""Source-aligned evidence and single-owner canonical publication planning.

These acceptance tests cover SA-01 and SA-02 prerequisites: source publications retain reversible
landed evidence and source semantics, every reference resolves exactly once, and one declared
component alone can produce one canonical plan from any number of source inputs.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from domain.semantics.curation import CanonicalCurationRequest, plan_curation
from domain.semantics.identity import (
    CommerceScopeKey,
    DeliveryScope,
    ExtractionProfileKey,
    SourceDatasetKey,
    SourceIdentifier,
    SourceSystemKey,
)
from domain.semantics.publication import (
    CanonicalDatasetKey,
    CanonicalPublisherAuthority,
    CanonicalPublisherBinding,
    CanonicalPublisherKey,
    PartitionIdentity,
    PublicationGraph,
    PublicationLensKey,
    PublicationOwnershipError,
    PublicationScope,
    SourcePublicationRef,
)
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.semantics.source_aligned import (
    LandedDeliveryRef,
    SourceAlignedField,
    SourceAlignedPublication,
    SourceAlignedRow,
    SourceAlignedRowset,
    SourceAlignedStateError,
    SourceInterpreterKey,
    SourceOption,
    resolve_source_inputs,
)
from domain.semantics.time import (
    BusinessCalendarKey,
    ExtractionCoverage,
    FactTimeClass,
    MeasurementBucket,
    SemanticTimeError,
    TimeGrain,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-044", "ADR-053")]

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
CALENDAR = BusinessCalendarKey("iso_gregorian", week_starts_on=0)
DATASET = CanonicalDatasetKey("fct_ad_exposure")
PUBLISHER = CanonicalPublisherKey("canonical-curation")


def _delivery_scope() -> DeliveryScope:
    return DeliveryScope(
        source_system=SourceSystemKey("amc"),
        source_dataset=SourceDatasetKey("dsp_impressions"),
        extraction_profile=ExtractionProfileKey("dsp_impressions_daily", version=3),
        source_scope=SourceIdentifier("amc.entity", "entity-123"),
        option_set_hash="options-sha256",
    )


def _measurement(day: date = date(2026, 8, 25)) -> MeasurementBucket:
    return MeasurementBucket(
        window=DateRange(day, day),
        grain=TimeGrain.DAY,
        calendar=CALENDAR,
        source_timezone="America/Los_Angeles",
    )


def _rowset() -> SourceAlignedRowset:
    return SourceAlignedRowset(
        delivery_scope=_delivery_scope(),
        extraction_coverage=ExtractionCoverage(DateRange(date(2026, 8, 24), date(2026, 8, 25))),
        interpreter=SourceInterpreterKey("amc.csv", version=2),
        source_options=(SourceOption("time_window", "2026-08-24/2026-08-25"),),
        rows=(
            SourceAlignedRow(
                fields=(
                    SourceAlignedField("campaign_id", "campaign-1"),
                    SourceAlignedField("impression_cost", Decimal("123000.0000")),
                ),
                source_identifiers=(SourceIdentifier("amc.campaign_id", "campaign-1"),),
                measurement=_measurement(),
            ),
        ),
    )


def _source(reference: str) -> SourceAlignedPublication:
    return SourceAlignedPublication(
        reference=SourcePublicationRef(reference),
        landed_delivery=LandedDeliveryRef(
            manifest_key=f"raw/{reference}/manifest.json",
            manifest_version=f"manifest-version-{reference}",
            manifest_checksum=f"manifest-checksum-{reference}",
            object_key=f"raw/{reference}/rows.csv",
            object_version=f"object-version-{reference}",
            object_checksum=f"checksum-{reference}",
        ),
        rowset=_rowset(),
        source_revision=SourceRevision.instant(
            scheme=RevisionSchemeKey("amc.execution"),
            source_id=f"execution-{reference}",
            observed_at=NOW,
        ),
        published_at=NOW,
    )


def _partition() -> PartitionIdentity:
    return PartitionIdentity(
        scope=PublicationScope(
            dataset=DATASET,
            commerce_scope=CommerceScopeKey("amazon_us"),
            population_key="advertising",
            lens=PublicationLensKey("default"),
            time_class=FactTimeClass.DAILY_ADDITIVE,
            business_calendar=CALENDAR,
            source_timezone="America/Los_Angeles",
        ),
        bucket=_measurement(),
    )


def _graph(*bindings: CanonicalPublisherBinding) -> PublicationGraph:
    return PublicationGraph(
        expected_datasets=frozenset({DATASET}),
        bindings=bindings,
    )


class TestSourceAlignedEvidence:
    def test_rowset_retains_source_scope_options_identifiers_time_and_interpreter(self) -> None:
        publication = _source("source-one")

        assert publication.landed_delivery.manifest_key.endswith("/manifest.json")
        assert publication.rowset.delivery_scope.source_system == SourceSystemKey("amc")
        assert publication.rowset.delivery_scope.source_dataset == SourceDatasetKey(
            "dsp_impressions"
        )
        assert publication.rowset.delivery_scope.option_set_hash == "options-sha256"
        assert publication.rowset.interpreter == SourceInterpreterKey("amc.csv", version=2)
        assert publication.rowset.rows[0].source_identifiers == (
            SourceIdentifier("amc.campaign_id", "campaign-1"),
        )
        assert publication.rowset.rows[0].measurement.grain is TimeGrain.DAY
        assert publication.content_checksum.startswith("sha256:")
        assert publication.row_count == 1

    def test_digest_is_deterministic_and_changes_with_interpretation_version(self) -> None:
        first = _rowset()
        same = _rowset()
        changed = replace(first, interpreter=SourceInterpreterKey("amc.csv", version=3))

        assert first.content_checksum == same.content_checksum
        assert changed.content_checksum != first.content_checksum

    def test_digest_ignores_semantically_irrelevant_collection_order(self) -> None:
        rowset = _rowset()
        row = rowset.rows[0]
        reordered = replace(
            rowset,
            rows=(
                replace(
                    row,
                    fields=tuple(reversed(row.fields)),
                    source_identifiers=tuple(reversed(row.source_identifiers)),
                ),
            ),
            source_options=tuple(reversed(rowset.source_options)),
        )
        assert reordered.content_checksum == rowset.content_checksum

    @pytest.mark.parametrize(
        "value",
        [
            {"nested": "value"},
            float("nan"),
            float("inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ],
    )
    def test_non_scalar_and_non_finite_values_are_refused(self, value: object) -> None:
        with pytest.raises(SourceAlignedStateError, match=r"supported scalar|finite"):
            SourceAlignedField("invalid", value)  # type: ignore[arg-type]

    def test_row_measurement_outside_extraction_coverage_is_refused(self) -> None:
        with pytest.raises(SemanticTimeError, match="outside extraction coverage"):
            replace(
                _rowset(),
                rows=(
                    SourceAlignedRow(
                        fields=(),
                        source_identifiers=(),
                        measurement=_measurement(date(2026, 8, 26)),
                    ),
                ),
            )

    def test_missing_and_ambiguous_source_references_are_refused(self) -> None:
        source = _source("source-one")
        with pytest.raises(SourceAlignedStateError, match="missing"):
            resolve_source_inputs((SourcePublicationRef("missing"),), (source,))
        with pytest.raises(SourceAlignedStateError, match="ambiguously"):
            resolve_source_inputs((source.reference,), (source, source))


class TestCanonicalPublicationOwnership:
    def test_authority_cannot_be_constructed_or_retargeted_by_a_caller(self) -> None:
        with pytest.raises(PublicationOwnershipError, match="not issued"):
            CanonicalPublisherAuthority(DATASET, PUBLISHER, object())

        authority = _graph(CanonicalPublisherBinding(DATASET, PUBLISHER)).authorize(
            DATASET, PUBLISHER
        )
        with pytest.raises(TypeError, match="dataclass"):
            replace(  # type: ignore[arg-type]
                authority,  # type: ignore[arg-type]
                publisher=CanonicalPublisherKey("attacker"),
            )
        with pytest.raises(AttributeError, match="immutable"):
            authority._publisher = CanonicalPublisherKey(  # type: ignore[reportPrivateUsage]
                "attacker"
            )

    def test_graph_requires_exactly_one_owner_for_every_expected_dataset(self) -> None:
        with pytest.raises(PublicationOwnershipError, match="missing"):
            _graph()
        with pytest.raises(PublicationOwnershipError, match="more than one"):
            _graph(
                CanonicalPublisherBinding(DATASET, PUBLISHER),
                CanonicalPublisherBinding(DATASET, CanonicalPublisherKey("another-publisher")),
            )
        with pytest.raises(PublicationOwnershipError, match="extra"):
            PublicationGraph(
                expected_datasets=frozenset({DATASET}),
                bindings=(
                    CanonicalPublisherBinding(DATASET, PUBLISHER),
                    CanonicalPublisherBinding(
                        CanonicalDatasetKey("extra_dataset"),
                        PUBLISHER,
                    ),
                ),
            )

    def test_wrong_component_cannot_plan_canonical_publication(self) -> None:
        source = _source("source-one")
        with pytest.raises(PublicationOwnershipError, match="owned by"):
            plan_curation(
                CanonicalCurationRequest(
                    partition=_partition(),
                    publisher=CanonicalPublisherKey("source-contract"),
                    source_references=(source.reference,),
                    available_sources=(source,),
                    graph=_graph(CanonicalPublisherBinding(DATASET, PUBLISHER)),
                )
            )

    def test_many_source_publications_produce_one_owner_authorized_plan(self) -> None:
        first = _source("source-one")
        second = _source("source-two")
        plan = plan_curation(
            CanonicalCurationRequest(
                partition=_partition(),
                publisher=PUBLISHER,
                source_references=(first.reference, second.reference),
                available_sources=(second, first),
                graph=_graph(CanonicalPublisherBinding(DATASET, PUBLISHER)),
            )
        )

        assert plan.partition.scope.dataset == DATASET
        assert plan.publisher == PUBLISHER
        assert plan.source_publications == (first, second)
        assert plan.source_references == (first.reference, second.reference)


def test_source_scope_is_checksum_bound() -> None:
    rowset = _rowset()
    changed = replace(
        rowset,
        delivery_scope=replace(
            rowset.delivery_scope,
            source_scope=SourceIdentifier("amc.entity", "fictional-other-entity"),
        ),
    )
    assert changed.content_checksum != rowset.content_checksum
