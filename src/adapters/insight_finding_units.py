# SPDX-License-Identifier: MIT-0
"""The unit, operand basis and prior scale of every insight finding (design §4.3, ADR-076 §5).

Builders name each finding's artifact metric id and basis; this module resolves its unit and
prior scale from the metric dictionary (`units`, `kind` and `ratio_scale`) through one declared
map from artifact ids to dictionary ids. `ALLOWED_FINDING_UNITS` declares, per slot, every
(finding kind, artifact metric id, basis) its builder can emit, so an undeclared triple raises
`InvariantError` in `make verify`, never in dev.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Final

from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog, MetricKind, RatioScale
from domain.narrative.shaped_response import UnitKind
from domain.presentation.insights import (
    FindingUnit,
    InsightFindingKind,
    InsightSlotId,
    MetricScale,
    OperandBasis,
)
from domain.presentation.metric_focus import (
    CAMPAIGN_DETAIL_FOCUS_METRICS,
    CAMPAIGN_PORTFOLIO_FOCUS_METRICS,
    RETAIL_FOCUS_METRICS,
)
from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "ALLOWED_FINDING_UNITS",
    "FINDING_METRIC_DICTIONARY_IDS",
    "dictionary_id",
    "finding_unit",
    "metric_scale",
]

FINDING_UNIT_UNDECLARED = ErrorCode("INSIGHT", 14)
"""A builder emitted a (kind, metric, basis) its slot does not declare."""

FINDING_METRIC_DICTIONARY_IDS: Final[Mapping[str, str]] = {
    "spend": "ad_spend",
    "attributed_roas": "roas_attributed_14d",
}
"""Artifact metric ids that are not dictionary ids; every other artifact id is one."""

_ABS = InsightFindingKind.ABSOLUTE_CHANGE
_PCT = InsightFindingKind.PERCENTAGE_CHANGE
_SHARE = InsightFindingKind.SHARE_OF_TOTAL
_RANKED = InsightFindingKind.RANKED_ABSOLUTE_CHANGE
_CAMPAIGN = OperandBasis.CAMPAIGN_WINDOW
_PORTFOLIO = OperandBasis.PORTFOLIO_WINDOW
_RETAIL = OperandBasis.RETAIL_WINDOW
_PRODUCT = OperandBasis.PRODUCT_WINDOW
_DAY = OperandBasis.CAMPAIGN_DAY
_CHANNEL = OperandBasis.CHANNEL_WINDOW
_GROUP = OperandBasis.GROUP_DELIVERY
_BUCKET = OperandBasis.BUCKET_DELIVERY
_COHORT = OperandBasis.COHORT_DELIVERY
_SEGMENT = OperandBasis.SEGMENT_DELIVERY

_PORTFOLIO_FOCUS: Final = CAMPAIGN_PORTFOLIO_FOCUS_METRICS
_CAMPAIGN_FOCUS: Final = CAMPAIGN_DETAIL_FOCUS_METRICS
_RETAIL_FOCUS: Final = RETAIL_FOCUS_METRICS


def _changes(
    metrics: tuple[str, ...], basis: OperandBasis
) -> frozenset[tuple[InsightFindingKind, str, OperandBasis]]:
    return frozenset((kind, metric, basis) for metric in metrics for kind in (_ABS, _PCT))


ALLOWED_FINDING_UNITS: Final[
    Mapping[InsightSlotId, frozenset[tuple[InsightFindingKind, str, OperandBasis]]]
] = {
    InsightSlotId.CONTRIBUTION: _changes(("spend",), _CAMPAIGN),
    InsightSlotId.DIVERGENCE: _changes(("portfolio_roas_attributed_14d",), _PORTFOLIO)
    | _changes(("attributed_roas",), _CAMPAIGN),
    InsightSlotId.EXPOSURE: frozenset({(_SHARE, "spend", _CAMPAIGN)}),
    InsightSlotId.MOVERS: frozenset({(_RANKED, "spend", _CAMPAIGN)}),
    InsightSlotId.TREND_SHAPE: _changes(("purchases",), _DAY),
    InsightSlotId.EFFICIENCY_DRIFT: _changes(("attributed_roas",), _CAMPAIGN),
    InsightSlotId.FUNNEL: frozenset({(_SHARE, "purchases", _CAMPAIGN)}),
    InsightSlotId.NEXT_CHECK: _changes(("purchases", "attributed_roas", "spend"), _CAMPAIGN),
    InsightSlotId.METRIC_FOCUS: _changes(_PORTFOLIO_FOCUS, _PORTFOLIO)
    | _changes(_CAMPAIGN_FOCUS, _CAMPAIGN)
    # Ranked drivers carry the contributor's metric (or the focused one): campaign drivers,
    # ratio components and day steps. They are context; no focus contract requires the kind.
    | frozenset((_RANKED, metric, _CAMPAIGN) for metric in (*_CAMPAIGN_FOCUS, *_PORTFOLIO_FOCUS))
    | frozenset((_RANKED, metric, _PORTFOLIO) for metric in _PORTFOLIO_FOCUS),
    InsightSlotId.RETAIL_METRIC_FOCUS: _changes(_RETAIL_FOCUS, _RETAIL)
    | frozenset(
        (_RANKED, metric, basis) for metric in _RETAIL_FOCUS for basis in (_RETAIL, _PRODUCT)
    ),
    InsightSlotId.DEMAND_SHAPE: _changes(
        ("retail_revenue_shipped", "units_shipped", "glance_views"), _RETAIL
    ),
    InsightSlotId.RETURNS_PRESSURE: _changes(
        ("customer_returns", "return_rate", "returns_value_estimate_retail"), _RETAIL
    ),
    InsightSlotId.ORDERED_VS_SHIPPED: _changes(("revenue_ordered",), _RETAIL),
    InsightSlotId.RETAIL_MOVERS: frozenset({(_RANKED, "retail_revenue_shipped", _PRODUCT)}),
    # Each finding is inside one network and one channel (ADR-106): a channel's share of its
    # network's spend, that channel's attributed ROAS move against its equal prior, and its
    # spend move, which only the governed alert rules read (UI slice 9a, "Also changed").
    InsightSlotId.NETWORK_SCORECARD: frozenset(
        {
            (_SHARE, "ad_spend", _CHANNEL),
            (_PCT, "roas_attributed_14d", _CHANNEL),
            (_PCT, "ad_spend", _CHANNEL),
        }
    ),
    # One ad product's new-to-brand revenue share move inside one network (ntb-share-mover).
    InsightSlotId.PORTFOLIO_ACQUISITION: frozenset({(_ABS, "ntb_revenue_share", _CHANNEL)}),
    # Inside one exposure-groups delivery on account IDs (ADR-101): DSP-only shoppers as a share
    # of every group's, and both groups' purchaser rate against sponsored only's.
    InsightSlotId.REACH_OVERLAP: frozenset({(_SHARE, "shoppers_reached", _GROUP)}),
    InsightSlotId.FULL_FUNNEL_ASSOCIATION: frozenset({(_PCT, "purchaser_rate", _GROUP)}),
    # Inside one frequency delivery on account IDs: the shoppers who saw more ads than the
    # bucket where the purchaser rate stops rising, as a share of every bucket's.
    InsightSlotId.FREQUENCY_SATURATION: frozenset(
        {(_SHARE, "shoppers_in_frequency_bucket", _BUCKET)}
    ),
    # One acquisition group's month-1 or month-3 repeat rate, latest cohort against the one
    # before it, inside one cohort vintage on the standard path.
    InsightSlotId.RETENTION_SHIFT: frozenset({(_ABS, "repeat_buyer_rate", _COHORT)}),
    # One segment's buyer share minus its reached share inside one buyer-profile delivery.
    InsightSlotId.SEGMENT_OVER_INDEX: frozenset({(_ABS, "segment_buyer_share", _SEGMENT)}),
}
"""Every (kind, artifact metric id, basis) each slot's builder can emit (design §4.3)."""


@functools.cache
def _catalog() -> Catalog:
    return load_catalog(contracts_root())


def dictionary_id(metric_id: str) -> str:
    """Translate an artifact metric id into its metric-dictionary id."""
    return FINDING_METRIC_DICTIONARY_IDS.get(metric_id, metric_id)


def metric_scale(metric_id: str, catalog: Catalog | None = None) -> MetricScale:
    """Return a metric's scale from the dictionary: its units, kind and ratio scale."""
    declared = (catalog or _catalog()).metric(dictionary_id(metric_id))
    if declared is None:
        raise InvariantError(
            FINDING_UNIT_UNDECLARED,
            "An insight finding names a metric the dictionary does not declare.",
            context={"metric": metric_id},
        )
    if declared.unit_kind is UnitKind.CURRENCY:
        return MetricScale.UNIT_PRICE if declared.kind is MetricKind.RATIO else MetricScale.CURRENCY
    if declared.unit_kind is UnitKind.COUNT:
        return MetricScale.COUNT
    return (
        MetricScale.RATE_PP if declared.ratio_scale is RatioScale.RATE else MetricScale.MULTIPLIER
    )


_SCALE_UNITS: Final[Mapping[MetricScale, FindingUnit]] = {
    MetricScale.CURRENCY: FindingUnit.CURRENCY,
    MetricScale.UNIT_PRICE: FindingUnit.UNIT_PRICE,
    MetricScale.COUNT: FindingUnit.COUNT,
    MetricScale.RATE_PP: FindingUnit.RATE_PP,
    MetricScale.MULTIPLIER: FindingUnit.MULTIPLIER,
}


def finding_unit(  # noqa: PLR0913 — one finding's coordinates, by keyword
    slot_id: InsightSlotId,
    kind: InsightFindingKind,
    *,
    metric_id: str,
    basis: OperandBasis,
    catalog: Catalog | None = None,
) -> tuple[FindingUnit, MetricScale | None]:
    """Return a finding's unit and, for a percentage finding, its prior scale.

    A percentage finding is in `percent` and a share in `share`, whatever the metric; an
    absolute or ranked change is in its metric's own scale.
    """
    if (kind, metric_id, basis) not in ALLOWED_FINDING_UNITS.get(slot_id, frozenset()):
        raise InvariantError(
            FINDING_UNIT_UNDECLARED,
            "An insight builder emitted a finding its slot does not declare.",
            context={
                "slot": slot_id.value,
                "kind": kind.value,
                "metric": metric_id,
                "basis": basis.value,
            },
        )
    scale = metric_scale(metric_id, catalog)
    if kind is InsightFindingKind.PERCENTAGE_CHANGE:
        return FindingUnit.PERCENT, scale
    if kind is InsightFindingKind.SHARE_OF_TOTAL:
        return FindingUnit.SHARE, None
    return _SCALE_UNITS[scale], None
