# SPDX-License-Identifier: MIT-0
"""Contract-driven Retail Health insight request derivation from the served artifact.

ADR-068 semantics identical to the campaign derivations: arithmetic is server work,
findings come only from the values the page displays (the artifact's totals and
reconciliation), Gate-1 predicates decide admission, and the cache key derives from
the artifact bytes without insight delivery state. The three prompted retail slots
(`demand-shape`, `returns-pressure`, `ordered-vs-shipped`) are declared in
``contracts/insights/``; ``retail-movers`` stays deterministic and bake-rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from adapters.campaign_insight_requests import (
    CampaignInsightRequestDerivation,
    DeclaredInsightDerivationInput,
    FindingSource,
    artifact_revision,
    change_findings,
    change_priors,
    declared_derivation,
    driver_context,
    evidence_bundle,
    predicate_findings,
)
from adapters.insight_next_steps import comparison_window_entry, data_sufficiency_entry
from domain.catalog.capability import RatioScale
from domain.narrative.shaped_response import UnitKind
from domain.presentation.currency_display import currency_display_text
from domain.presentation.insights import (
    InsightContextEntry,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightSlotId,
    OperandBasis,
    insight_window_profile,
)
from domain.presentation.metric_focus import (
    all_product_drivers,
    retail_health_focus_ingredients,
)
from domain.presentation.retail_health import (
    RetailComparison,
    RetailHealthArtifact,
    RetailMetricValue,
)
from domain.presentation.retail_health import (
    retail_health_artifact_payload as _retail_payload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from domain.catalog.capability import Catalog
    from domain.presentation.campaign_attribution import CampaignNumber
    from domain.presentation.insights import InsightContract
    from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope

RETAIL_INSIGHT_SUBJECT = "retail-health"
_DEMAND_METRICS = ("retail_revenue_shipped", "units_shipped", "glance_views")
_RETURNS_METRICS = ("customer_returns", "return_rate", "returns_value_estimate_retail")


@dataclass(frozen=True, slots=True)
class RetailInsightRequestDerivationInput:
    """The served Retail Health artifact and the identity required for its insight slots."""

    artifact: RetailHealthArtifact
    scope: AuthorizedAnalyticalScope
    contracts: Mapping[InsightSlotId, InsightContract]
    catalog: Catalog


def retail_payload_without_ephemera(artifact: RetailHealthArtifact) -> dict[str, object]:
    """Return the measured retail bytes without insight delivery state."""
    payload = _retail_payload(artifact)
    return {key: value for key, value in payload.items() if key != "insights"}


def _label(catalog: Catalog, metric_id: str) -> str:
    declared = catalog.metric(metric_id)
    return metric_id if declared is None else declared.label


def _comparison_operands(
    catalog: Catalog,
    totals: Mapping[str, RetailComparison],
    metrics: tuple[str, ...],
) -> tuple[tuple[str, CampaignNumber, CampaignNumber], ...]:
    """Project the displayed current/prior totals into named change operands."""
    operands: list[tuple[str, CampaignNumber, CampaignNumber]] = []
    for metric_id in metrics:
        comparison = totals.get(metric_id)
        if comparison is None:
            continue
        operands.append(
            (
                _label(catalog, metric_id),
                Decimal(str(comparison.current.value)),
                Decimal(str(comparison.prior.value)),
            )
        )
    return tuple(operands)


def _operand_metrics(catalog: Catalog, metrics: tuple[str, ...]) -> dict[str, str]:
    """Map each operand's display label back to the artifact metric id it measures."""
    return {_label(catalog, metric_id): metric_id for metric_id in metrics}


def _percent_text(value: Decimal) -> str:
    """Render a rate as the tile does: the exact ratio times 100, at two places."""
    return f"{(value * 100).quantize(Decimal('0.01'))}%"


def _is_rate(catalog: Catalog, metric_id: str) -> bool:
    declared = catalog.metric(metric_id)
    return declared is not None and declared.ratio_scale is RatioScale.RATE


def _display(catalog: Catalog, metric_id: str, value: RetailMetricValue) -> str:
    """Render one served value in the unit its tile shows (F16: never a bare rate ratio)."""
    number = Decimal(str(value.value))
    if _is_rate(catalog, metric_id):
        return _percent_text(number)
    if value.units.kind is UnitKind.CURRENCY and value.units.currency_code is not None:
        return currency_display_text(number, value.units.currency_code)
    return str(value.value)


def _totals_context(
    catalog: Catalog,
    totals: Mapping[str, RetailComparison],
    metrics: tuple[str, ...],
) -> tuple[InsightContextEntry, ...]:
    """State each total current vs prior in its tile's unit; a rate also states its pp move."""
    entries: list[InsightContextEntry] = []
    for metric_id in metrics:
        if metric_id not in totals:
            continue
        comparison = totals[metric_id]
        text = (
            f"{_display(catalog, metric_id, comparison.current)} vs "
            f"{_display(catalog, metric_id, comparison.prior)}"
        )
        if _is_rate(catalog, metric_id):
            points = (
                (Decimal(str(comparison.current.value)) - Decimal(str(comparison.prior.value)))
                * 100
            ).quantize(Decimal("0.01"))
            text += f" ({'+' if points > 0 else ''}{points} percentage points)"
        entries.append(
            InsightContextEntry(f"{_label(catalog, metric_id)} (current vs prior)", text)
        )
    return tuple(entries)


RETURNS_ESTIMATE_DEFINITION = (
    "Estimated revenue withheld by returns is returned units times shipped revenue per unit "
    "shipped; it is not the value of the items returned."
)
"""C4: what the returns estimate is, so it is never read as the returned items' value."""


def _returns_operand_context(
    catalog: Catalog, totals: Mapping[str, RetailComparison]
) -> tuple[InsightContextEntry, ...]:
    """C4: the estimate's operands, current vs prior, as context (never Gate-1 findings)."""
    entries = [InsightContextEntry("returns estimate definition", RETURNS_ESTIMATE_DEFINITION)]
    entries.extend(_totals_context(catalog, totals, ("units_shipped", "retail_revenue_shipped")))
    revenue, units = totals.get("retail_revenue_shipped"), totals.get("units_shipped")
    if revenue is not None and units is not None:
        sides = [
            (Decimal(str(revenue_side.value)), Decimal(str(units_side.value)))
            for revenue_side, units_side in (
                (revenue.current, units.current),
                (revenue.prior, units.prior),
            )
        ]
        code = revenue.current.units.currency_code
        if all(count > 0 for _, count in sides):
            current, prior = (amount / count for amount, count in sides)
            entries.append(
                InsightContextEntry(
                    "shipped revenue per unit (current vs prior)",
                    f"{currency_display_text(current, code)} vs "
                    f"{currency_display_text(prior, code)}",
                )
            )
    return tuple(entries)


def _all_products_context(
    artifact: RetailHealthArtifact, metric: str
) -> tuple[InsightContextEntry, ...]:
    """FR-11 (R6-5): every product's change with the dialog's summary lines.

    Supplied only when the served rows are every product in view, so the list is complete.
    """
    if not artifact.rows or artifact.total_count != len(artifact.rows):
        return ()
    try:
        ingredients = retail_health_focus_ingredients(artifact, metric)
    except ValueError:
        return ()
    built = all_product_drivers(ingredients)
    return () if built is None else driver_context(*built)


def retail_insight_request_derivation(
    request: RetailInsightRequestDerivationInput,
) -> CampaignInsightRequestDerivation:
    """Derive the three prompted Retail Health slots from the served artifact only."""
    artifact = request.artifact
    catalog = request.catalog
    profile = insight_window_profile(artifact.period, artifact.prior_period)
    totals = {item.metric: item for item in artifact.totals}
    window_context = (
        InsightContextEntry(
            "reporting window", f"{artifact.period.start} to {artifact.period.end}"
        ),
        comparison_window_entry(artifact.prior_period),
        data_sufficiency_entry(item for row in artifact.rows for item in row.freshness_qualifiers),
        # Plain words, never the wire token: the model echoes what it is shown, and
        # a narration that received the raw enum served it back to the customer.
        InsightContextEntry("distributor view", artifact.distributor_view.customer_words),
    )

    demand_source = FindingSource(
        InsightSlotId.DEMAND_SHAPE,
        OperandBasis.RETAIL_WINDOW,
        _operand_metrics(catalog, _DEMAND_METRICS),
    )
    returns_source = FindingSource(
        InsightSlotId.RETURNS_PRESSURE,
        OperandBasis.RETAIL_WINDOW,
        _operand_metrics(catalog, _RETURNS_METRICS),
    )
    # Ordered against shipped revenue: both operands are currency totals; the finding is
    # named for the ordered side it measures.
    ordered_source = FindingSource(
        InsightSlotId.ORDERED_VS_SHIPPED, OperandBasis.RETAIL_WINDOW, "revenue_ordered"
    )
    demand_operands = _comparison_operands(catalog, totals, _DEMAND_METRICS)
    demand_absolute = change_findings(demand_operands, InsightFindingKind.ABSOLUTE_CHANGE)
    demand_percentage = change_findings(demand_operands, InsightFindingKind.PERCENTAGE_CHANGE)

    returns_operands = _comparison_operands(catalog, totals, _RETURNS_METRICS)
    returns_absolute = change_findings(returns_operands, InsightFindingKind.ABSOLUTE_CHANGE)
    returns_percentage = change_findings(returns_operands, InsightFindingKind.PERCENTAGE_CHANGE)

    # Ordered-versus-shipped compares the two displayed current-window revenue totals;
    # under SOURCING the reconciliation is a typed absence, so the slot classifies as
    # ineligible (missing required evidence) while its served state stays silent.
    ordered = totals.get("revenue_ordered")
    shipped = totals.get("retail_revenue_shipped")
    ordered_operands: tuple[tuple[str, CampaignNumber, CampaignNumber], ...] = ()
    if ordered is not None and shipped is not None:
        ordered_operands = (
            (
                "Ordered versus shipped revenue",
                Decimal(str(ordered.current.value)),
                Decimal(str(shipped.current.value)),
            ),
        )
    ordered_absolute = change_findings(ordered_operands, InsightFindingKind.ABSOLUTE_CHANGE)
    ordered_percentage = change_findings(ordered_operands, InsightFindingKind.PERCENTAGE_CHANGE)
    # Availability declares what this artifact actually delivered, never the static
    # contract shape (declared-outcomes rule): a slot whose displayed totals carry
    # none of its metrics was not evaluated, so it must classify as ineligible.
    demand_inputs: tuple[InsightEvidenceInput, ...] = (
        (InsightEvidenceInput.RETAIL_TOTALS,) if demand_operands else ()
    )
    returns_inputs: tuple[InsightEvidenceInput, ...] = (
        (InsightEvidenceInput.RETAIL_TOTALS,) if returns_operands else ()
    )
    reconciliation_inputs: tuple[InsightEvidenceInput, ...] = (
        (InsightEvidenceInput.RETAIL_TOTALS,) if totals else ()
    ) + (
        (InsightEvidenceInput.RETAIL_RECONCILIATION,) if artifact.reconciliation is not None else ()
    )
    reconciliation_context: tuple[InsightContextEntry, ...] = (
        (
            InsightContextEntry(
                "reconciliation",
                f"{artifact.reconciliation.outcome} as of {artifact.reconciliation.as_of}",
            ),
        )
        if artifact.reconciliation is not None
        else ()
    )

    return declared_derivation(
        DeclaredInsightDerivationInput(
            scope=request.scope,
            subject=RETAIL_INSIGHT_SUBJECT,
            window=artifact.period,
            window_profile=profile,
            artifact_revision=artifact_revision(retail_payload_without_ephemera(artifact)),
            slot_ids=(
                InsightSlotId.DEMAND_SHAPE,
                InsightSlotId.RETURNS_PRESSURE,
                InsightSlotId.ORDERED_VS_SHIPPED,
            ),
            findings_by_slot={
                InsightSlotId.DEMAND_SHAPE: demand_absolute + demand_percentage,
                InsightSlotId.RETURNS_PRESSURE: returns_absolute + returns_percentage,
                InsightSlotId.ORDERED_VS_SHIPPED: ordered_absolute + ordered_percentage,
            },
            evidence_by_slot={
                InsightSlotId.DEMAND_SHAPE: evidence_bundle(
                    demand_inputs,
                    predicate_findings(
                        demand_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        InsightEvidenceInput.RETAIL_TOTALS,
                        source=demand_source,
                    )
                    + predicate_findings(
                        demand_percentage,
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        InsightEvidenceInput.RETAIL_TOTALS,
                        source=demand_source,
                        priors=change_priors(demand_operands),
                    ),
                ),
                InsightSlotId.RETURNS_PRESSURE: evidence_bundle(
                    returns_inputs,
                    predicate_findings(
                        returns_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        InsightEvidenceInput.RETAIL_TOTALS,
                        source=returns_source,
                    )
                    + predicate_findings(
                        returns_percentage,
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        InsightEvidenceInput.RETAIL_TOTALS,
                        source=returns_source,
                        priors=change_priors(returns_operands),
                    ),
                ),
                InsightSlotId.ORDERED_VS_SHIPPED: evidence_bundle(
                    reconciliation_inputs,
                    predicate_findings(
                        ordered_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        *reconciliation_inputs,
                        source=ordered_source,
                    )
                    + predicate_findings(
                        ordered_percentage,
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        *reconciliation_inputs,
                        source=ordered_source,
                        priors=change_priors(ordered_operands),
                    ),
                ),
            },
            contracts=request.contracts,
            display_context_by_slot={
                InsightSlotId.DEMAND_SHAPE: (
                    *window_context,
                    *_totals_context(catalog, totals, _DEMAND_METRICS),
                    *_all_products_context(artifact, "units_shipped"),
                ),
                InsightSlotId.RETURNS_PRESSURE: (
                    *window_context,
                    *_totals_context(catalog, totals, _RETURNS_METRICS),
                    *_returns_operand_context(catalog, totals),
                    *_all_products_context(artifact, "customer_returns"),
                ),
                InsightSlotId.ORDERED_VS_SHIPPED: (
                    *window_context,
                    *_totals_context(
                        catalog, totals, ("revenue_ordered", "retail_revenue_shipped")
                    ),
                    *reconciliation_context,
                ),
            },
        )
    )
