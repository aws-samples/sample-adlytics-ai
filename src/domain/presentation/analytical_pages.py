# SPDX-License-Identifier: MIT-0
"""Closed typed analytical page results composed from shaped measurements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domain.narrative.publication import AnswerQualifier
from domain.narrative.shaped_response import ClaimField, ShapedResponse, Units
from domain.presentation.page_schemas import AnalyticalPageSchema, MetricRole
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.compatibility import CompatibilityDifference
from domain.semantics.identity import PlatformKey
from domain.semantics.media_taxonomy import MediaTaxonomy
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange

__all__ = [
    "AnalyticalPageResult",
    "AvailabilityState",
    "PageMeasurementResult",
    "PageMeasurementResultRequest",
    "PresentationAnswer",
    "PresentationData",
    "PresentationDimensionLabel",
    "PresentationMetadata",
    "campaign_platform_labels",
    "presentation_dimension_labels",
    "project_presentation_answer",
]

_FORBIDDEN_SELECTOR_FIELDS = frozenset(
    {
        "amazon_asin",
        "campaign_key",
        "channel_key",
        "marketplace_key",
        "platform_key",
    }
)


class AvailabilityState(StrEnum):
    """What the selected publication and query established for one metric slot."""

    AVAILABLE = "available"
    EMPTY = "empty"
    PARTIAL = "partial"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class PageMeasurementResultRequest:
    """Typed inputs for deriving one page measurement result."""

    metric_name: str
    roles: tuple[MetricRole, ...]
    variant_id: str | None
    answer: ShapedResponse


@dataclass(frozen=True, slots=True)
class PageMeasurementResult:
    """One governed metric variant and its already-shaped answer."""

    metric_name: str
    roles: tuple[MetricRole, ...]
    variant_id: str | None
    availability: AvailabilityState
    answer: ShapedResponse

    @classmethod
    def from_answer(cls, request: PageMeasurementResultRequest) -> PageMeasurementResult:
        """Derive availability only from the shaped answer's typed metadata."""
        answer = request.answer
        if answer.metadata.suppression.rows_suppressed:
            availability = AvailabilityState.SUPPRESSED
        elif answer.metadata.truncated:
            availability = AvailabilityState.PARTIAL
        elif answer.metadata.row_count == 0:
            availability = AvailabilityState.EMPTY
        else:
            availability = AvailabilityState.AVAILABLE
        return cls(
            request.metric_name,
            request.roles,
            request.variant_id,
            availability,
            answer,
        )


@dataclass(frozen=True, slots=True)
class AnalyticalPageResult:
    """One complete page; construction requires every declared metric variant."""

    schema: AnalyticalPageSchema
    measurements: tuple[PageMeasurementResult, ...]

    def __post_init__(self) -> None:
        expected = tuple(
            (metric.name, variant.variant_id if variant is not None else None)
            for metric in self.schema.metrics
            for variant in (metric.variants or (None,))
        )
        actual = tuple(
            (measurement.metric_name, measurement.variant_id) for measurement in self.measurements
        )
        if actual != expected:
            raise ValueError("analytical page measurements differ from its governed declaration.")


PresentationScalar = str | int | Decimal | None


@dataclass(frozen=True, slots=True)
class PresentationDimensionLabel:
    """One governed rewrite from an internal selector value to public display text."""

    selector_field: str
    selector_value: str
    display_field: str
    display_value: str

    def __post_init__(self) -> None:
        public_fields = {
            "channel_key": "channel",
            "marketplace_key": "marketplace",
            "platform_key": "platform",
        }
        if public_fields.get(self.selector_field) != self.display_field:
            raise ValueError("presentation labels require one governed public dimension pair.")
        if not self.selector_value.strip() or not self.display_value.strip():
            raise ValueError("presentation labels require a selector value and display text.")


def campaign_platform_labels(taxonomy: MediaTaxonomy) -> tuple[PresentationDimensionLabel, ...]:
    """Project every governed platform term into its public display label."""
    return tuple(
        PresentationDimensionLabel("platform_key", term.key.value, "platform", term.name)
        for term in taxonomy.terms
        if isinstance(term.key, PlatformKey)
    )


def presentation_dimension_labels(
    taxonomy: MediaTaxonomy, bridge: CommerceIdentityBridge
) -> tuple[PresentationDimensionLabel, ...]:
    """Project every governed channel and marketplace selector into its public display label.

    One derivation shared by the request handler and the artifact bakers, so a baked
    signature and a redemption-time signature can never disagree on label projection.
    """
    return tuple(
        PresentationDimensionLabel(*item)
        for item in sorted(
            {
                *(
                    (
                        "channel_key",
                        mapping.resolution.channel.value,
                        "channel",
                        taxonomy.term_for(mapping.resolution.channel).name,
                    )
                    for mapping in taxonomy.mappings
                    if mapping.resolution.channel is not None
                ),
                *(
                    (
                        "marketplace_key",
                        name.marketplace.value,
                        "marketplace",
                        name.value,
                    )
                    for name in bridge.marketplace_display_names
                ),
            }
        )
    )


@dataclass(frozen=True, slots=True)
class PresentationData:
    """Closed browser data containing governed claims and display descriptors."""

    values: tuple[dict[str, PresentationScalar], ...]
    units: Units | None
    grain: tuple[str, ...]
    claim_fields: tuple[ClaimField, ...]
    descriptor_fields: tuple[str, ...]
    population_total: Decimal | int | None


@dataclass(frozen=True, slots=True)
class PresentationMetadata:
    """Complete browser-relevant honesty metadata without tool or selector details."""

    coverage_start: datetime
    coverage_end: datetime
    requested_coverage_intervals: tuple[DateRange, ...]
    actual_coverage_intervals: tuple[DateRange, ...]
    coverage_holes: tuple[DateRange, ...]
    actual_coverage_start: datetime | None
    actual_coverage_end: datetime | None
    freshness_status: str
    freshness_reason: str | None
    loaded_at: datetime | None
    source_timezone: str
    suppression: Suppression
    row_count: int
    computed_at: datetime
    semantic_qualifiers: tuple[CompatibilityDifference, ...]
    qualified_dq: tuple[str, ...]
    qualifiers: tuple[AnswerQualifier, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class PresentationAnswer:
    """Selector-safe typed projection of one shaped analytical answer."""

    data: PresentationData
    metadata: PresentationMetadata


def project_presentation_answer(
    answer: ShapedResponse,
    page_grain: tuple[str, ...],
    labels: tuple[PresentationDimensionLabel, ...] = (),
) -> PresentationAnswer:
    """Project typed tool evidence with governed labels replacing internal selectors."""
    label_index = {(label.selector_field, label.selector_value): label for label in labels}
    if len(label_index) != len(labels):
        raise ValueError("presentation labels repeat an internal selector value.")
    public_fields = {
        "channel_key": "channel" if "channel" in page_grain else None,
        "marketplace_key": "marketplace" if "marketplace" in page_grain else None,
    }
    claims = tuple(
        field for field in answer.data.claim_fields if field.name not in _FORBIDDEN_SELECTOR_FIELDS
    )
    projections: list[tuple[str, str, str | None]] = [
        (field.name, field.name, None) for field in claims
    ]
    descriptors: list[str] = []
    descriptor_sources = dict.fromkeys(
        (
            *(field.name for field in answer.data.descriptor_fields),
            *answer.data.grain.dimensions,
        )
    )
    for source_name in descriptor_sources:
        if source_name in {field.name for field in claims}:
            continue
        if source_name not in _FORBIDDEN_SELECTOR_FIELDS:
            descriptors.append(source_name)
            projections.append((source_name, source_name, None))
            continue
        display_field = public_fields.get(source_name)
        if display_field is not None:
            descriptors.append(display_field)
            projections.append((source_name, display_field, display_field))

    values: list[dict[str, PresentationScalar]] = []
    for row in answer.data.values:
        projected: dict[str, PresentationScalar] = {}
        for source_name, target_name, required_dimension in projections:
            value = row[source_name]
            if required_dimension is None:
                projected[target_name] = value
                continue
            if not isinstance(value, str):
                raise ValueError("a public presentation dimension requires a text selector.")
            label = label_index.get((source_name, value))
            if label is None or label.display_field != required_dimension:
                raise ValueError("a public presentation dimension lacks one governed label.")
            projected[target_name] = label.display_value
        values.append(projected)

    metadata = answer.metadata
    return PresentationAnswer(
        data=PresentationData(
            values=tuple(values),
            units=answer.data.units,
            grain=page_grain,
            claim_fields=claims,
            descriptor_fields=tuple(descriptors),
            population_total=answer.data.population_total,
        ),
        metadata=PresentationMetadata(
            coverage_start=metadata.coverage_start,
            coverage_end=metadata.coverage_end,
            requested_coverage_intervals=metadata.requested_coverage_intervals,
            actual_coverage_intervals=metadata.actual_coverage_intervals,
            coverage_holes=metadata.coverage_holes,
            actual_coverage_start=metadata.actual_coverage_start,
            actual_coverage_end=metadata.actual_coverage_end,
            freshness_status=metadata.freshness_status.value,
            freshness_reason=metadata.freshness_reason,
            loaded_at=metadata.loaded_at,
            source_timezone=metadata.source_timezone,
            suppression=metadata.suppression,
            row_count=metadata.row_count,
            computed_at=metadata.computed_at,
            semantic_qualifiers=metadata.semantic_qualifiers,
            qualified_dq=metadata.qualified_dq,
            qualifiers=metadata.qualifiers,
            truncated=metadata.truncated,
        ),
    )
