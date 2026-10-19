# SPDX-License-Identifier: MIT-0
"""The metrics the presentation serves with a comparison, and their change semantics.

ADR-121 decision 2: every metric served with a comparison declares `favourable_direction` in
the metric dictionary, and loading fails when one doesn't. The roster is the union of every
comparison the served pages carry: Campaigns tiles and rows, campaign detail's summary and
delivery, Retail health tiles and product rows, product detail's ad-attributed channels, and the
metric focus drivers, whose ratio
components and day steps are these same metrics plus the estimate's average selling price.
"""

from __future__ import annotations

from domain.catalog.capability import Catalog
from domain.presentation.campaign_attribution import (
    CAMPAIGN_SUMMARY_GOVERNED_METRICS,
    CampaignDeliverySlot,
)
from domain.presentation.campaign_portfolio import CAMPAIGN_PORTFOLIO_GOVERNED_METRICS
from domain.presentation.change import ChangeSemantics, change_semantics_from
from domain.presentation.metric_focus import FOCUS_DERIVED_ASP_METRIC
from domain.presentation.new_customers import NEW_CUSTOMERS_COMPARED_METRICS
from domain.presentation.retail_health import (
    RETAIL_HEALTH_ROW_METRICS,
    RETAIL_HEALTH_TOTAL_METRICS,
)
from domain.presentation.retail_product_detail import RETAIL_PRODUCT_AD_METRICS
from domain.presentation.scorecard import SCORECARD_COMPARED_METRICS

__all__ = [
    "PRESENTATION_COMPARED_METRICS",
    "new_customers_change_semantics",
    "presentation_change_semantics",
    "presentation_labels",
    "scorecard_change_semantics",
]

PRESENTATION_COMPARED_METRICS = frozenset(
    {
        *CAMPAIGN_PORTFOLIO_GOVERNED_METRICS.values(),
        *CAMPAIGN_SUMMARY_GOVERNED_METRICS.values(),
        *(slot.value for slot in CampaignDeliverySlot),
        *RETAIL_HEALTH_TOTAL_METRICS,
        *RETAIL_HEALTH_ROW_METRICS,
        *RETAIL_PRODUCT_AD_METRICS,
        FOCUS_DERIVED_ASP_METRIC,
    }
)
"""Dictionary metric ids that a served page compares across two windows."""

PRESENTATION_RESCALED_RATES = frozenset({"conversion_rate_retail"})
"""Rates a page shows rescaled, not as a percentage, so their change reads in percent.

Retail health shows retail conversion as units per 1,000 glance views, and the approved mockup
reads its change as a percent ("+0.1%"); points of a percentage the page never shows would not.
"""


def presentation_change_semantics(catalog: Catalog) -> ChangeSemantics:
    """Load the compared roster's semantics; refuses a compared metric without the field."""
    return change_semantics_from(
        catalog, PRESENTATION_COMPARED_METRICS, relative=PRESENTATION_RESCALED_RATES
    )


def presentation_labels(catalog: Catalog) -> dict[str, str]:
    """Load each compared metric's governed display label (ADR-067), keyed by dictionary id.

    The coverage block names every figure with these labels (ADR-119), so a figure missing from
    the dictionary fails loading rather than reaching the page as a code.
    """
    labels: dict[str, str] = {}
    for metric_id in sorted(PRESENTATION_COMPARED_METRICS):
        declared = catalog.metric(metric_id)
        if declared is None or not declared.label.strip():
            raise ValueError(f"compared metric {metric_id!r} has no dictionary label.")
        labels[metric_id] = declared.label
    return labels


def scorecard_change_semantics(catalog: Catalog) -> ChangeSemantics:
    """Load the Scorecard's compared figures' semantics (ADR-121), refusing a missing field.

    The Scorecard's roster is its own, not part of `PRESENTATION_COMPARED_METRICS`: the
    Campaigns and Retail health render formats track that union's tone, and neither page bakes
    the deduplicated headline or TACoS, so adding them there would re-bake both pages for no
    rendered change. The Scorecard's own render format tracks this roster.
    """
    return change_semantics_from(catalog, SCORECARD_COMPARED_METRICS)


def new_customers_change_semantics(catalog: Catalog) -> ChangeSemantics:
    """Load New customers' compared figure's semantics (ADR-121), refusing a missing field.

    Its roster is its own, as the Scorecard's is: adding `ntb_revenue_share` to
    `PRESENTATION_COMPARED_METRICS` would move the Campaigns and Retail health render formats for
    no rendered change on either page.
    """
    return change_semantics_from(catalog, NEW_CUSTOMERS_COMPARED_METRICS)
