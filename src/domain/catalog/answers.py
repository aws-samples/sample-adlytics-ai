# SPDX-License-Identifier: MIT-0
"""Builds the ShapedResponse a catalog question is answered with.

In domain rather than in the tool adapter, so the narrative scaffolding and the refusal
wording are unit-testable without reading a file — and so the adapter is only wiring.

Four envelope fields have no obvious value for a tool that reads no fact, and getting them
right is much of the reason build-order 2.6a puts this first, while changing the envelope is
still cheap. Each choice is argued where it is made below:

- `facts_read` names the facts the described metrics *would* read.
- `measurement_basis` is the union across those metrics, so `mixes_basis` becomes true for a
  mixed listing — which is correct rather than a false positive.
- `coverage_start` and `coverage_end` are both the moment of computation.
- `units` describes the response's own figures, not the metrics it describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from domain.catalog.capability import Catalog, CatalogError, DeferredMetric, Metric
from domain.narrative.publication import CollectionCompletenessV1, GroundedCollectionV1
from domain.narrative.shaped_response import (
    DescriptorField,
    FreshnessStatus,
    Metadata,
    ResponseData,
    ShapedResponse,
    Suppression,
    UnitKind,
    Units,
)
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import Grain

__all__ = [
    "AnswerContext",
    "DescribeMetricRequest",
    "ListMetricsRequest",
    "describe_metric",
    "list_metrics",
]

type Row = dict[str, str | int | Decimal | None]
"""One row of a capability answer.

Matches `ResponseData.values` exactly rather than narrowing it. The narrower
`str | int | None` looks harmless and is not assignable: `dict` is invariant in its value
type, so a narrower row is rejected where the envelope expects the full union."""

METRIC_GRAIN = Grain(("metric",))
"""One row is one metric. A capability answer still has a grain — the envelope makes grain
mandatory precisely so that "a correct figure at an unstated grain" is impossible, and that
applies to a list of metrics as much as to a measurement."""

CAPABILITY_UNITS = Units(kind=UnitKind.COUNT)
"""The response's own figures are counts of things, not measurements.

`ResponseData.units` denominates the response, and a capability answer carries no measured
quantity: the only number in it is how many metrics there are. Each row separately states
the units *its* metric would be denominated in, as text, because that is a description
rather than a figure. Setting this to the units of one described metric would be the
mistake — a listing spanning currency, count and ratio has no single denomination.
"""


def _descriptor_fields(rows: tuple[Row, ...]) -> tuple[DescriptorField, ...]:
    """Declare the catalog row vocabulary as structured descriptive evidence."""
    if not rows:
        return ()
    return tuple(DescriptorField(name) for name in rows[0])


# The catalogue ships with the code, so it is never stale in the sense the freshness registry
# measures. A loaded fact can lag its source; a declaration cannot.
CAPABILITY_FRESHNESS = FreshnessStatus.FRESH

# No fact was read, so no row was withheld. Distinct from "we did not check".
NO_SUPPRESSION = Suppression(rows_suppressed=False)


@dataclass(frozen=True, slots=True)
class AnswerContext:
    """Per-request values every answer carries, whatever was asked."""

    computed_at: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        if self.computed_at.tzinfo is None:
            raise CatalogError("computed_at must be timezone-aware.")
        if not self.correlation_id:
            raise CatalogError("correlation_id is required on every answer.")


@dataclass(frozen=True, slots=True)
class ListMetricsRequest:
    """Ask what can be asked."""

    catalog: Catalog
    context: AnswerContext


@dataclass(frozen=True, slots=True)
class DescribeMetricRequest:
    """Ask what one metric means, and optionally whether a grain is supported."""

    catalog: Catalog
    context: AnswerContext
    metric_name: str
    grain: str = ""
    """When given, the answer also decides whether the metric is valid at this grain."""

    def __post_init__(self) -> None:
        if not self.metric_name:
            raise CatalogError("A describe request must name a metric.")


def _metadata(options: _MetadataOptions) -> Metadata:
    """Assemble the metadata every catalog answer shares."""
    return Metadata(
        # The envelope requires a response to name what it read, and a capability answer is
        # genuinely *about* these facts: a caller learns which tables an answer would come
        # from before asking for it. The fallback is a guard, not an expected path — every
        # declared metric resolves to at least one fact, and a response that resolved none
        # should say so rather than fail an invariant a reader cannot act on.
        facts_read=options.facts_read or ("(no fact resolved)",),
        measurement_basis=options.bases,
        # A capability answer describes the catalogue as it is now, not a window of data.
        # Both ends are the moment of computation: a zero-width window is the honest
        # statement that no period was covered, where inventing a range would imply data
        # was consulted. The real window arrives with the coverage half (build-order 2.6b).
        coverage_start=options.context.computed_at,
        coverage_end=options.context.computed_at,
        freshness_status=CAPABILITY_FRESHNESS,
        suppression=NO_SUPPRESSION,
        row_count=options.row_count,
        computed_at=options.context.computed_at,
        correlation_id=options.context.correlation_id,
        contract_ids=options.contract_ids,
    )


@dataclass(frozen=True, slots=True)
class _MetadataOptions:
    """Everything `_metadata` needs, so adding one costs a field and not a parameter."""

    context: AnswerContext
    facts_read: tuple[str, ...]
    bases: tuple[MeasurementBasis, ...]
    row_count: int
    contract_ids: tuple[str, ...] = ()


def _row_for(catalog: Catalog, metric: Metric) -> Row:
    """Build a compact row for the metric inventory."""
    return {
        "metric": metric.name,
        "label": metric.label,
        "kind": metric.kind.value,
        "units": metric.unit_kind.value,
        "measurement_basis": ", ".join(basis.value for basis in metric.measurement_basis),
        "valid_grains": ", ".join(metric.valid_grains),
        "reads": ", ".join(catalog.facts_read(metric)),
        "status": "available",
    }


def _describe_row(catalog: Catalog, metric: Metric) -> Row:
    """Build the single-metric projection, including only governed customer copy."""
    return {
        **_row_for(catalog, metric),
        "customer_definition": metric.customer_definition or None,
        "customer_caveat": metric.customer_caveat or None,
    }


def _inventory_collections(
    catalog: Catalog, deferred: tuple[DeferredMetric, ...]
) -> tuple[GroundedCollectionV1, ...]:
    """Project the canonical inventory partition without asking the model to regroup it."""
    available = tuple(
        GroundedCollectionV1(
            collection_id=f"catalog:{category.category_id}",
            heading=category.label,
            items=tuple(
                metric.label
                for metric_id in category.metric_ids
                for metric in (catalog.metric(metric_id),)
                if metric is not None
            ),
            completeness=CollectionCompletenessV1.COMPLETE,
            source_paths=(f"catalog.inventory_categories.{category.category_id}",),
            required=True,
            count_label="metrics",
        )
        for category in catalog.ordered_inventory_categories()
    )
    if not deferred:
        return available
    pending = GroundedCollectionV1(
        collection_id="catalog:not_yet_computable",
        heading="Declared but not yet computable",
        items=tuple(metric.label for metric in deferred),
        completeness=CollectionCompletenessV1.COMPLETE,
        source_paths=("catalog.deferred",),
        required=True,
        count_label="metrics",
    )
    return (*available, pending)


def list_metrics(request: ListMetricsRequest) -> ShapedResponse:
    """Answer "what can you tell me" from the declared metrics."""
    catalog = request.catalog
    available = catalog.metrics
    deferred = tuple(sorted(catalog.deferred, key=lambda metric: metric.name))

    collections = _inventory_collections(catalog, deferred)
    rows: tuple[Row, ...] = tuple(
        {
            "collection": collection.collection_id,
            "label": collection.heading,
            "status": (
                "not_yet_computable"
                if collection.collection_id == "catalog:not_yet_computable"
                else "available"
            ),
        }
        for collection in collections
    )

    facts: list[str] = []
    for metric in available:
        for fact in catalog.facts_read(metric):
            if fact not in facts:
                facts.append(fact)

    bases = catalog.bases_across(available)

    narrative = [
        (
            f"The platform declares {len(available) + len(deferred)} metrics: "
            f"{len(available)} are currently computable and {len(deferred)} are declared "
            "but not yet computable."
        ),
        "Each metric's supported breakdowns are available through the metric description "
        "operation. A request outside them is refused with a reason rather than approximated.",
    ]
    if deferred:
        narrative.append(
            f"{len(deferred)} further metrics are declared but not yet computable: "
            f"{', '.join(metric.label for metric in deferred)}. They are named so they read "
            "as pending rather than unknown."
        )
    if len(set(bases)) > 1:
        # Not a false positive on a capability answer. These metrics come from different
        # measurement systems, those systems do not reconcile with each other by design, and
        # a user choosing between them should know that before asking.
        narrative.append(
            "These metrics span more than one measurement system. Figures from different "
            "systems are not expected to reconcile with each other."
        )
    narrative.append(
        "This describes what the platform can compute. Whether a given period is loaded is "
        "a separate question this answer does not address."
    )

    return ShapedResponse(
        data=ResponseData(
            values=rows,
            units=CAPABILITY_UNITS,
            grain=METRIC_GRAIN,
            descriptor_fields=(),
            claim_narrative_indices=(0,),
            collections=collections,
        ),
        narrative=tuple(narrative),
        metadata=_metadata(
            _MetadataOptions(
                context=request.context,
                facts_read=tuple(facts),
                bases=bases,
                row_count=len(rows),
            )
        ),
    )


def describe_metric(request: DescribeMetricRequest) -> ShapedResponse:
    """Answer what one metric means, or raise the refusal that says why not.

    Raises rather than returning an empty response, for the reason argued on
    `Refusal.to_error`: a refusal ran no query, so a response would have to invent the
    measurement basis and coverage window `Metadata` requires.
    """
    catalog = request.catalog
    # A customer-facing label or governed alias resolves to its canonical name through the
    # same surfaces the measurement resolver reads, so this tool is never stricter than that
    # one. Several matches deliberately fall through unresolved: the unknown-metric refusal
    # then names what exists, which is the conservative answer to an ambiguous alias.
    candidates = catalog.metric_candidates(request.metric_name)
    metric_name = candidates[0].name if len(candidates) == 1 else request.metric_name
    refusal = catalog.refusal_for(metric_name, grain=request.grain or None)
    if refusal is not None:
        raise refusal.to_error()

    metric = catalog.metric(metric_name)
    if metric is None:  # pragma: no cover - refusal_for already returned for this case
        raise CatalogError(f"{metric_name} produced no refusal and no metric, which cannot happen.")

    facts = catalog.facts_read(metric)
    narrative = [
        f"{metric.label} is denominated in {metric.unit_kind.value}.",
        f"It is valid at these grains: {', '.join(metric.valid_grains)}.",
        f"It reads {', '.join(facts)}." if facts else "It reads no canonical fact directly.",
    ]
    if request.grain:
        narrative.append(f"{request.grain} grain is supported for this metric.")
    if metric.definition:
        narrative.append(f"It is computed as {metric.definition}.")
    if metric.is_default_roas:
        narrative.append("This is the default ROAS.")
    if metric.includes_brand_halo:
        # Reads higher than the default by construction, so presenting it alone overstates.
        narrative.append(
            "This figure includes brand halo, so it reads higher than the attributed "
            "default. The default should be shown alongside it."
        )
    if metric.mixes_basis or metric.declared_mixes_basis:
        narrative.append(
            "This metric spans more than one measurement system, so it is an efficiency "
            "ratio rather than attribution and cannot be reconciled against either system "
            "on its own."
        )
    if metric.notes:
        narrative.append(metric.notes.strip())

    row = _describe_row(catalog, metric)
    return ShapedResponse(
        data=ResponseData(
            values=(row,),
            units=CAPABILITY_UNITS,
            grain=METRIC_GRAIN,
            descriptor_fields=_descriptor_fields((row,)),
        ),
        narrative=tuple(narrative),
        metadata=_metadata(
            _MetadataOptions(
                context=request.context,
                facts_read=facts,
                bases=metric.measurement_basis,
                row_count=1,
            )
        ),
    )
