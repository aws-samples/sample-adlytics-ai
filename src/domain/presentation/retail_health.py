# SPDX-License-Identifier: MIT-0
# ruff: noqa: D101, PLR2004, S101, E731
# pyright: reportArgumentType=false, reportCallIssue=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownVariableType=false, reportUnnecessaryCast=false
"""Closed Retail Health artifact (V2) and exact capability-free payload codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

from domain.measurement.comparison import absolute_change, percentage_change
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.change import (
    CHANGE_KEY,
    PERCENTAGE_KEY,
    Change,
    ChangeOperands,
    ChangeSemantics,
    build_change,
    change_from_payload,
    check_change,
    comparison_change_payload,
    comparison_fields,
    require_change_blocks,
)
from domain.presentation.currency_display import (
    DISPLAY_VALUE_KEY,
    carries_display_value,
    display_value_mode,
    metric_display_value,
)
from domain.presentation.evidence_status import (
    EVIDENCE_STATUS_KEY,
    CoverageFact,
    CoverageKind,
    EvidenceStatus,
    evidence_status_from_payload,
    evidence_status_payload,
    labels_for,
    require_entries,
    withheld_slots,
    write_evidence_status,
)
from domain.presentation.insights import (
    InsightSlot,
    InsightSlotId,
    insight_slots_from_payload,
    insight_slots_payload,
)
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain

RETAIL_HEALTH_ARTIFACT_TYPE = "RetailHealthArtifact"
RETAIL_HEALTH_ARTIFACT_VERSION = 2
RETAIL_HEALTH_MAX_ROWS = 50
RETAIL_HEALTH_TOTAL_GRAIN = Grain(("period", "marketplace"))
RETAIL_HEALTH_ROW_GRAIN = Grain(("period", "product", "marketplace"))
RETAIL_HEALTH_TREND_GRAIN = Grain(("date", "marketplace"))
_RETAIL_INSIGHT_IDS = (
    InsightSlotId.DEMAND_SHAPE,
    InsightSlotId.RETURNS_PRESSURE,
    InsightSlotId.ORDERED_VS_SHIPPED,
    InsightSlotId.RETAIL_MOVERS,
)
_RETAIL_INSIGHTS = tuple(slot.value for slot in _RETAIL_INSIGHT_IDS)
_TOTAL_METRICS = (
    "retail_revenue_shipped",
    "units_shipped",
    "customer_returns",
    "glance_views",
    "revenue_ordered",
    "cogs_shipped",
    "conversion_rate_retail",
    # Derived returns-and-margin compare slots (design revision 2026-09-17.2). Appended
    # rather than interleaved so the V1→V2 widening reads as an extension on the wire; the
    # narrative sections regroup tiles by name, never by roster position.
    "return_rate",
    "returns_value_estimate_retail",
    "fulfilment_rate",
    "amazon_margin_proxy",
    # The four-group tiles shared with product detail (render format 8, ADR-125 decision 4):
    # ordered units, average selling price and revenue per glance view, appended again.
    "units_ordered",
    "asp_shipped",
    "revenue_per_glance_view",
)
_TREND_METRICS = ("retail_revenue_shipped", "units_shipped", "customer_returns", "glance_views")
# The metrics a SOURCING pull structurally cannot carry: ordered figures are null under
# SOURCING (units_ordered, revenue_ordered, and every derivation over them), and the traffic
# report has no distributorView so conversion and revenue per glance view are only coherent
# under MANUFACTURING. Under SOURCING each is a typed absence, never a zero.
_SOURCING_WITHHELD = (
    "revenue_ordered",
    "conversion_rate_retail",
    "fulfilment_rate",
    "units_ordered",
    "revenue_per_glance_view",
)
_SIGNED_METRICS = frozenset({"amazon_margin_proxy"})
RETURN_REASONS_ABSENCE = "Vendor Central doesn't send return reasons for this account."
"""Return reasons have no governed source, so both retail pages state them as a typed absence."""
AMC_RETAIL_PURCHASES_PAID = (
    "Retail purchases from Amazon Marketing Cloud, a second view of the same sales, need the "
    "Amazon Retail Purchases paid feature. No data has arrived for this workspace. When it "
    "does, they show beside the Vendor Central figures with the difference stated, never "
    "added to them."
)
"""The paid-feature status both retail pages serve (ia §8.4, §5.3 item 21)."""
_ABSENCE_METRICS = frozenset(
    {
        *_SOURCING_WITHHELD,
        # The returns family is null under the FRESH selling program; the absence reason
        # carries the plain-words selling-program statement.
        "customer_returns",
        "return_rate",
        "returns_value_estimate_retail",
    }
)
_METRIC_UNITS = {
    "retail_revenue_shipped": UnitKind.CURRENCY,
    "units_shipped": UnitKind.COUNT,
    "customer_returns": UnitKind.COUNT,
    "glance_views": UnitKind.COUNT,
    "revenue_ordered": UnitKind.CURRENCY,
    "cogs_shipped": UnitKind.CURRENCY,
    "conversion_rate_retail": UnitKind.RATIO,
    "return_rate": UnitKind.RATIO,
    "returns_value_estimate_retail": UnitKind.CURRENCY,
    "fulfilment_rate": UnitKind.RATIO,
    "amazon_margin_proxy": UnitKind.CURRENCY,
    "units_ordered": UnitKind.COUNT,
    "asp_shipped": UnitKind.CURRENCY,
    "revenue_per_glance_view": UnitKind.CURRENCY,
}
_ROW_METRICS = (
    "retail_revenue_shipped",
    "units_shipped",
    "customer_returns",
    "glance_views",
    "return_rate",
    "amazon_margin_proxy",
)


def retail_health_expected_totals(
    distributor_view: RetailDistributorView,
    absent_metrics: frozenset[str],
) -> tuple[str, ...]:
    """Return the exact totals roster a complete artifact must carry for one view.

    A typed absence replaces its compare slot rather than coexisting with a zero, so the
    expected roster is the declared roster minus exactly the absent metrics.
    """
    withheld = set(absent_metrics)
    if distributor_view is RetailDistributorView.SOURCING:
        withheld.update(_SOURCING_WITHHELD)
    return tuple(metric for metric in _TOTAL_METRICS if metric not in withheld)


RETAIL_HEALTH_INSIGHT_IDS = _RETAIL_INSIGHT_IDS
RETAIL_HEALTH_TOTAL_METRICS = _TOTAL_METRICS
RETAIL_HEALTH_TREND_METRICS = _TREND_METRICS
RETAIL_HEALTH_ROW_METRICS = _ROW_METRICS
RETAIL_HEALTH_METRIC_UNITS = _METRIC_UNITS
RETAIL_HEALTH_ABSENCE_METRICS = _ABSENCE_METRICS
RETAIL_HEALTH_SOURCING_WITHHELD = _SOURCING_WITHHELD


class RetailHealthOutcome(StrEnum):
    COMPLETE = "complete"
    EMPTY = "empty"
    INSUFFICIENT = "insufficient"


class RetailDistributorView(StrEnum):
    MANUFACTURING = "MANUFACTURING"
    SOURCING = "SOURCING"

    @property
    def customer_words(self) -> str:
        """The plain words every customer- or model-visible surface uses for this view.

        The wire token itself must never reach a page or a prompt: a model echoes what
        it is shown, and a narration that received the raw enum served it back to the
        customer verbatim (found live, 2026-09-24). The frontend band pill renders the
        same words from the wire enum (RetailHealthView.tsx); reword both together.
        """
        return (
            "Manufacturing view" if self is RetailDistributorView.MANUFACTURING else "Sourcing view"
        )


class RetailHealthSortField(StrEnum):
    SHIPPED_REVENUE = "retail_revenue_shipped"
    UNITS_SHIPPED = "units_shipped"
    CUSTOMER_RETURNS = "customer_returns"
    GLANCE_VIEWS = "glance_views"


@dataclass(frozen=True, slots=True)
class RetailHealthSort:
    """One declared server sort over the artifact's sortable row columns."""

    field: RetailHealthSortField
    direction: str

    def __post_init__(self) -> None:
        if self.direction not in {"asc", "desc"}:
            raise ValueError("Retail Health sort direction is invalid.")


def _previous(period: DateRange) -> DateRange:
    end = period.start - timedelta(days=1)
    return DateRange(end - timedelta(days=period.days - 1), end)


def _number(value: int | str | Decimal, label: str, *, signed: bool = False) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric.")
    try:
        numeric = Decimal(value)
    except InvalidOperation, ValueError:
        raise ValueError(f"{label} must be numeric.") from None
    if not numeric.is_finite() or (not signed and numeric < 0):
        raise ValueError(f"{label} has an invalid sign or value.")


def _payload_number(value: int | str | Decimal) -> int | str:
    return value if isinstance(value, int) else str(value)


def _period_payload(period: DateRange) -> dict[str, str]:
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _previous_check(current: DateRange, prior: DateRange) -> None:
    if prior != _previous(current):
        raise ValueError("Retail Health comparisons must use the equal preceding period.")


@dataclass(frozen=True, slots=True)
class RetailMetricValue:
    value: int | str | Decimal
    units: Units
    period: DateRange
    grain: Grain
    basis: MeasurementBasis = MeasurementBasis.RETAIL_ACTUAL
    signed: bool = False
    """Whether a negative value is meaningful. Construction-time only, never serialized.

    True only for the Amazon margin proxy, where revenue below wholesale cost is a real
    figure rather than a defect; every other retail value stays non-negative.
    """

    def __post_init__(self) -> None:
        _number(self.value, "retail metric value", signed=self.signed)
        if self.basis is not MeasurementBasis.RETAIL_ACTUAL:
            raise ValueError("Retail Health values must have retail-actual basis.")


@dataclass(frozen=True, slots=True)
class RetailComparison:
    metric: str
    current: RetailMetricValue
    prior: RetailMetricValue
    absolute_change: int | str | Decimal | None
    """The exact delta; `None` only where the change is withheld."""
    percentage_change: str | None
    change: Change | None = None
    """The server-declared change (ADR-121); `None` only on a withheld change."""
    withheld: bool = False
    """A missing or partial day in this figure's window withholds its change (ADR-118 decision
    5): no delta, percentage or block is served, and the coverage block says why."""

    def __post_init__(self) -> None:
        if self.metric not in _METRIC_UNITS:
            raise ValueError("Retail Health metric is not declared.")
        if self.metric not in _SIGNED_METRICS and (self.current.signed or self.prior.signed):
            raise ValueError("Retail Health metric cannot carry a signed value.")
        if (
            self.current.units != self.prior.units
            or self.current.basis is not self.prior.basis
            or self.current.grain != self.prior.grain
        ):
            raise ValueError("Retail Health comparison semantics differ.")
        _previous_check(self.current.period, self.prior.period)
        if self.current.units.kind is not _METRIC_UNITS[self.metric]:
            raise ValueError("Retail Health metric units are not declared.")
        if self.withheld:
            if (
                self.absolute_change is not None
                or self.percentage_change is not None
                or self.change is not None
            ):
                raise ValueError("A withheld Retail Health comparison serves no change.")
            return
        if self.absolute_change is None:
            raise ValueError("Retail Health comparison delta is not exact.")
        _number(self.absolute_change, "retail comparison delta", signed=True)
        if Decimal(self.absolute_change) != Decimal(
            absolute_change(self.current.value, self.prior.value)
        ):
            raise ValueError("Retail Health comparison delta is not exact.")
        if self.percentage_change != percentage_change(self.current.value, self.prior.value):
            raise ValueError("Retail Health comparison percentage is not exact.")
        if self.change is not None:
            check_change(
                self.change,
                ChangeOperands(self.current.value, self.prior.value, self.current.units),
            )


def retail_comparison(  # noqa: PLR0913, PLR0917 -- a metric, its two sides, and their semantics
    metric: str,
    current: RetailMetricValue,
    prior: RetailMetricValue,
    semantics: ChangeSemantics,
) -> RetailComparison:
    """Compare two exact retail values, with the change block the dictionary governs."""
    return RetailComparison(
        metric,
        current,
        prior,
        absolute_change(current.value, prior.value),
        percentage_change(current.value, prior.value),
        build_change(
            ChangeOperands(current.value, prior.value, current.units),
            semantics.for_metric(metric),
        ),
    )


_RATIO_FIGURES = frozenset(
    {
        "conversion_rate_retail",
        "return_rate",
        "fulfilment_rate",
        "asp_shipped",
        "revenue_per_glance_view",
    }
)
"""Figures a missing or partial day leaves out of both parts; every other figure adds up."""


def retail_health_coverage_facts(
    metrics: tuple[str, ...], labels: Mapping[str, str]
) -> tuple[CoverageFact, ...]:
    """Name each served tile from its dictionary label, each covering its whole window.

    A bake measures only answers whose window is covered end to end, so its figures are
    complete; a missing or partial day is stated by replacing a fact's dates before
    `write_evidence_status` writes the block.
    """
    named = labels_for(metrics, labels, "Retail Health")
    return tuple(
        CoverageFact(
            metric,
            named[metric],
            "Headline and product rows" if metric in _ROW_METRICS else "Headline",
            CoverageKind.RATIO if metric in _RATIO_FIGURES else CoverageKind.ADDITIVE,
            entity_word="products",
        )
        for metric in metrics
    )


def retail_health_evidence_status(
    period: DateRange, metrics: tuple[str, ...], labels: Mapping[str, str]
) -> EvidenceStatus:
    """Write the coverage block of a body whose every tile covers its whole window."""
    return write_evidence_status(period, retail_health_coverage_facts(metrics, labels))


@dataclass(frozen=True, slots=True)
class RetailTypedAbsence:
    metric: str
    reason: str

    def __post_init__(self) -> None:
        if self.metric not in _ABSENCE_METRICS or not self.reason.strip():
            raise ValueError("Retail Health typed absence is invalid.")


@dataclass(frozen=True, slots=True)
class RetailTrendPoint:
    day: date
    current: tuple[RetailMetricValue, ...]
    prior: tuple[RetailMetricValue, ...]

    def __post_init__(self) -> None:
        if tuple(value.grain for value in self.current) != (RETAIL_HEALTH_TREND_GRAIN,) * len(
            _TREND_METRICS
        ):
            raise ValueError("Retail Health current trend grain is invalid.")
        if tuple(value.grain for value in self.prior) != (RETAIL_HEALTH_TREND_GRAIN,) * len(
            _TREND_METRICS
        ):
            raise ValueError("Retail Health prior trend grain is invalid.")
        if len(self.current) != len(_TREND_METRICS) or len(self.prior) != len(_TREND_METRICS):
            raise ValueError("Retail Health trend metric roster is invalid.")


@dataclass(frozen=True, slots=True)
class RetailProductRow:
    child_asin: str
    parent_asin: str
    title: str
    comparisons: tuple[RetailComparison, ...]
    shipped_revenue_spark: tuple[RetailMetricValue, ...]
    freshness_qualifiers: tuple[str, ...] = ()
    selection_reference: str | None = None
    """The opaque row capability the serve path minted for product detail (ADR-125 decision
    3). Never baked: a stored body's rows carry none, and the served row's title and child
    ASIN are its safe display signature."""

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.child_asin, self.parent_asin, self.title)):
            raise ValueError("Retail product identity is incomplete.")
        if self.selection_reference is not None and not self.selection_reference.strip():
            raise ValueError("Retail product selection reference must be nonblank.")
        if tuple(value.metric for value in self.comparisons) != _ROW_METRICS:
            raise ValueError("Retail product metric roster is invalid.")
        if any(value.current.grain != RETAIL_HEALTH_ROW_GRAIN for value in self.comparisons):
            raise ValueError("Retail product row grain is invalid.")
        if len(set(self.freshness_qualifiers)) != len(self.freshness_qualifiers) or any(
            not value.strip() for value in self.freshness_qualifiers
        ):
            raise ValueError("Retail product freshness qualifiers are invalid.")


@dataclass(frozen=True, slots=True)
class RetailReconciliation:
    outcome: str
    as_of: date
    product_revenue: int | str | Decimal | None = None
    reported_revenue: int | str | Decimal | None = None
    product_units: int | str | Decimal | None = None
    reported_units: int | str | Decimal | None = None

    def __post_init__(self) -> None:
        values = (
            self.product_revenue,
            self.reported_revenue,
            self.product_units,
            self.reported_units,
        )
        if self.outcome == "reconciled":
            if any(value is not None for value in values):
                raise ValueError(
                    "A reconciled Retail Health result does not carry discrepancy figures."
                )
        elif self.outcome == "discrepancy":
            if any(value is None for value in values):
                raise ValueError(
                    "A Retail Health discrepancy must show both revenue and unit figures."
                )
            for value in values:
                assert value is not None
                _number(value, "retail reconciliation value")
        else:
            raise ValueError("Retail Health reconciliation outcome is invalid.")


@dataclass(frozen=True, slots=True)
class RetailHealthArtifact:
    outcome: RetailHealthOutcome
    period: DateRange
    prior_period: DateRange
    distributor_view: RetailDistributorView
    totals: tuple[RetailComparison, ...]
    absences: tuple[RetailTypedAbsence, ...]
    insights: tuple[InsightSlot, ...]
    trend: tuple[RetailTrendPoint, ...]
    rows: tuple[RetailProductRow, ...]
    total_count: int
    truncation: int | None
    reconciliation: RetailReconciliation | None
    boundary: str
    kind: str = "retail_health"
    evidence_status: EvidenceStatus | None = None
    """The window's coverage block (ADR-119), on a complete body. `None` on an empty or
    insufficient body, which shows no tile."""

    def __post_init__(self) -> None:  # noqa: PLR0912
        if self.kind != "retail_health" or not self.boundary.strip():
            raise ValueError("Retail Health artifact identity is invalid.")
        _previous_check(self.period, self.prior_period)
        if self.total_count < 0 or len(self.rows) > RETAIL_HEALTH_MAX_ROWS:
            raise ValueError("Retail Health rows exceed their declared bound.")
        if tuple(slot.id.value for slot in self.insights) != _RETAIL_INSIGHTS:
            raise ValueError("Retail Health insight roster is invalid.")
        absent_metrics = frozenset(item.metric for item in self.absences)
        if len(absent_metrics) != len(self.absences):
            raise ValueError("Retail Health typed absences must be unique per metric.")
        if self.outcome is RetailHealthOutcome.COMPLETE:
            expected_totals = retail_health_expected_totals(self.distributor_view, absent_metrics)
            if (
                tuple(item.metric for item in self.totals) != expected_totals
                or self.reconciliation is None
                or not self.trend
            ):
                raise ValueError("Complete Retail Health artifact is incomplete.")
            if self.distributor_view is RetailDistributorView.SOURCING and not (
                set(_SOURCING_WITHHELD) <= absent_metrics
            ):
                raise ValueError("Sourcing must state its withheld figures as typed absences.")
            expected = tuple(
                self.period.start + timedelta(days=index) for index in range(self.period.days)
            )
            if tuple(point.day for point in self.trend) != expected:
                raise ValueError("Retail Health trend must cover the requested window exactly.")
            if self.total_count < len(self.rows):
                raise ValueError("Retail Health count cannot be smaller than displayed rows.")
        elif (
            self.totals
            or self.absences
            or self.trend
            or self.rows
            or self.total_count
            or self.truncation is not None
            or self.reconciliation is not None
        ):
            raise ValueError("Closed Retail Health artifact cannot carry presentation data.")
        if self.distributor_view is RetailDistributorView.MANUFACTURING:
            if absent_metrics & set(_SOURCING_WITHHELD):
                raise ValueError(
                    "Manufacturing view-dependent figures must be measured, not absent."
                )
        elif any(item.metric in _SOURCING_WITHHELD for item in self.totals):
            raise ValueError("Sourcing cannot carry a view-withheld figure.")
        if self.truncation is None:
            if self.total_count != len(self.rows):
                raise ValueError("Untruncated Retail Health rows must disclose the total.")
        elif (
            self.truncation != RETAIL_HEALTH_MAX_ROWS
            or len(self.rows) != self.truncation
            or self.total_count <= self.truncation
        ):
            raise ValueError("Retail Health truncation is invalid.")
        comparisons = (*self.totals, *(item for row in self.rows for item in row.comparisons))
        self._require_coverage(comparisons)
        require_change_blocks(
            (comparison.change for comparison in comparisons if not comparison.withheld),
            "Retail Health",
        )

    def _require_coverage(self, comparisons: tuple[RetailComparison, ...]) -> None:
        """Hold every withheld change to the coverage block, and the block to the tiles."""
        status = self.evidence_status
        if status is None:
            if any(item.withheld for item in comparisons):
                raise ValueError("A withheld Retail Health change requires its coverage block.")
            return
        if self.outcome is not RetailHealthOutcome.COMPLETE:
            raise ValueError("Only a complete Retail Health body serves a coverage block.")
        require_entries(
            status,
            tuple((item.metric, "current") for item in self.totals),
            self.period,
            label="Retail Health",
        )
        withheld = withheld_slots(status)
        for item in comparisons:
            if item.withheld != (item.metric in withheld):
                raise ValueError("A Retail Health change is withheld exactly where coverage says.")
            if not item.withheld and item.change is None:
                raise ValueError("A Retail Health body with coverage serves every change.")


def _units_payload(units: Units) -> dict[str, str | None]:
    return {"kind": units.kind.value, "currency_code": units.currency_code}


def _point_payload(value: RetailMetricValue) -> dict[str, object]:
    """Encode a plotted trend point or spark entry: the five fields, never a display value."""
    return {
        "basis": value.basis.value,
        "grain": list(value.grain.dimensions),
        "period": _period_payload(value.period),
        "units": _units_payload(value.units),
        "value": _payload_number(value.value),
    }


def _value_payload(value: RetailMetricValue) -> dict[str, object]:
    """Encode a displayed comparison value, with its server display value (ADR-084)."""
    return {
        **_point_payload(value),
        "display_value": metric_display_value(value.units, value.value),
    }


def _comparison_payload(value: RetailComparison) -> dict[str, object]:
    return {
        "metric": value.metric,
        "current": _value_payload(value.current),
        "prior": _value_payload(value.prior),
        "absolute_change": None
        if value.absolute_change is None
        else _payload_number(value.absolute_change),
        **comparison_change_payload(value.percentage_change, value.change),
    }


def retail_health_artifact_payload(artifact: RetailHealthArtifact) -> dict[str, object]:
    """Encode exact server-owned Retail Health values without capabilities or live inputs."""
    return {
        "kind": artifact.kind,
        "outcome": artifact.outcome.value,
        "period": _period_payload(artifact.period),
        "prior_period": _period_payload(artifact.prior_period),
        "distributor_view": artifact.distributor_view.value,
        "totals": [_comparison_payload(value) for value in artifact.totals],
        "absences": [
            {"metric": value.metric, "reason": value.reason} for value in artifact.absences
        ],
        "insights": insight_slots_payload(artifact.insights),
        "trend": [
            {
                "date": point.day.isoformat(),
                "current": [_point_payload(value) for value in point.current],
                "prior": [_point_payload(value) for value in point.prior],
            }
            for point in artifact.trend
        ],
        "rows": [
            {
                "child_asin": row.child_asin,
                "parent_asin": row.parent_asin,
                "title": row.title,
                "comparisons": [_comparison_payload(value) for value in row.comparisons],
                "shipped_revenue_spark": [
                    _point_payload(value) for value in row.shipped_revenue_spark
                ],
                "freshness_qualifiers": list(row.freshness_qualifiers),
                **(
                    {}
                    if row.selection_reference is None
                    else {"selection_reference": row.selection_reference}
                ),
            }
            for row in artifact.rows
        ],
        "total_count": artifact.total_count,
        "truncation": artifact.truncation,
        "reconciliation": None
        if artifact.reconciliation is None
        else {
            "outcome": artifact.reconciliation.outcome,
            "as_of": artifact.reconciliation.as_of.isoformat(),
            "product_revenue": None
            if artifact.reconciliation.product_revenue is None
            else _payload_number(artifact.reconciliation.product_revenue),
            "reported_revenue": None
            if artifact.reconciliation.reported_revenue is None
            else _payload_number(artifact.reconciliation.reported_revenue),
            "product_units": None
            if artifact.reconciliation.product_units is None
            else _payload_number(artifact.reconciliation.product_units),
            "reported_units": None
            if artifact.reconciliation.reported_units is None
            else _payload_number(artifact.reconciliation.reported_units),
        },
        "boundary": artifact.boundary,
        EVIDENCE_STATUS_KEY: evidence_status_payload(artifact.evidence_status),
        **retail_fixed_statuses_payload(),
    }


RETURN_REASONS_KEY = "return_reasons"
AMC_RETAIL_PURCHASES_KEY = "amc_retail_purchases"


def retail_fixed_statuses_payload() -> dict[str, object]:
    """Return the two server-written statuses both retail pages carry from render format 8."""
    return {
        RETURN_REASONS_KEY: {"reason": RETURN_REASONS_ABSENCE},
        AMC_RETAIL_PURCHASES_KEY: {"status": "paid_feature", "text": AMC_RETAIL_PURCHASES_PAID},
    }


def check_retail_fixed_statuses(item: Mapping[str, object], label: str) -> None:
    """Refuse a body whose fixed statuses differ from the server's one wording."""
    expected = retail_fixed_statuses_payload()
    if any(item.get(key) != value for key, value in expected.items()):
        raise ValueError(f"{label} fixed statuses are not canonical.")


def _object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} fields are not exact.")
    return cast("dict[str, object]", value)


def _items(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be ISO date.") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be canonical.")
    return parsed


def _period(value: object, label: str) -> DateRange:
    item = _object(value, {"start", "end"}, label)
    return DateRange(_date(item["start"], f"{label} start"), _date(item["end"], f"{label} end"))


def _numeric(value: object, label: str, *, signed: bool = False) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label} must be canonical number.")
    _number(value, label, signed=signed)
    if (
        isinstance(value, str)
        and str(Decimal(value)) != value
        and not ("." in value and value.rstrip("0").rstrip(".") != value)
    ):
        raise ValueError(f"{label} is not canonical.")
    return value


def _units(value: object, label: str) -> Units:
    item = _object(value, {"kind", "currency_code"}, label)
    try:
        kind = UnitKind(_text(item["kind"], f"{label} kind"))
    except ValueError:
        raise ValueError(f"{label} kind is unsupported.") from None
    currency = item["currency_code"]
    if currency is not None and (
        not isinstance(currency, str) or len(currency) != 3 or currency != currency.upper()
    ):
        raise ValueError(f"{label} currency is invalid.")
    return Units(kind, currency)


def _grain(value: object, label: str) -> Grain:
    values = _items(value, label)
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"{label} is invalid.")
    return Grain(tuple(cast("list[str]", values)))


_VALUE_FIELDS = frozenset({"basis", "grain", "period", "units", "value"})


def _metric_value(item: dict[str, object], label: str, *, signed: bool) -> RetailMetricValue:
    try:
        basis = MeasurementBasis(_text(item["basis"], f"{label} basis"))
    except ValueError:
        raise ValueError(f"{label} basis is unsupported.") from None
    return RetailMetricValue(
        _numeric(item["value"], f"{label} value", signed=signed),
        _units(item["units"], f"{label} units"),
        _period(item["period"], f"{label} period"),
        _grain(item["grain"], f"{label} grain"),
        basis,
        signed=signed,
    )


def _point_value(value: object, label: str) -> RetailMetricValue:
    """Decode a trend point or spark entry: exactly the five fields."""
    return _metric_value(_object(value, set(_VALUE_FIELDS), label), label, signed=False)


def _comparison_value(value: object, label: str, *, signed: bool = False) -> RetailMetricValue:
    """Decode a comparison value: five fields, or six with the server display value.

    A retained pre-display body carries five. This decoder has no re-encode, so a present
    display value is checked explicitly against the one derivation.
    """
    fields = set(_VALUE_FIELDS)
    if carries_display_value(value):
        fields.add(DISPLAY_VALUE_KEY)
    item = _object(value, fields, label)
    result = _metric_value(item, label, signed=signed)
    if DISPLAY_VALUE_KEY in item and item[DISPLAY_VALUE_KEY] != metric_display_value(
        result.units, result.value
    ):
        raise ValueError(f"{label} display value is not canonical.")
    return result


def _comparison_metric_values(item: dict[str, object]) -> list[object]:
    """Collect the comparison values a body displays; plotted points never carry the key."""
    comparisons: list[object] = list(_items(item["totals"], "retail totals"))
    for row in _items(item["rows"], "retail rows"):
        if isinstance(row, dict):
            raw = cast("dict[str, object]", row).get("comparisons")
            if isinstance(raw, list):
                comparisons.extend(cast("list[object]", raw))
    return [
        cast("dict[str, object]", comparison).get(side)
        for comparison in comparisons
        if isinstance(comparison, dict)
        for side in ("current", "prior")
    ]


def _comparison(value: object, label: str) -> RetailComparison:
    # Every comparison carries the change key; the retained form without one is refused.
    fields = comparison_fields(value, frozenset({"metric", "current", "prior", "absolute_change"}))
    item = _object(value, set(fields), label)
    metric = _text(item["metric"], f"{label} metric")
    signed = metric in _SIGNED_METRICS
    current = _comparison_value(item["current"], f"{label} current", signed=signed)
    prior = _comparison_value(item["prior"], f"{label} prior", signed=signed)
    percentage = (
        item[PERCENTAGE_KEY]
        if PERCENTAGE_KEY in item
        else percentage_change(current.value, prior.value)
    )
    if percentage is not None and not isinstance(percentage, str):
        raise ValueError(f"{label} percentage is invalid.")
    # No delta is a withheld change; the artifact holds it to the coverage block.
    if item["absolute_change"] is None:
        if item[CHANGE_KEY] is not None or percentage is not None:
            raise ValueError(f"{label} withholds its delta but serves a change.")
        return RetailComparison(metric, current, prior, None, None, None, withheld=True)
    return RetailComparison(
        metric,
        current,
        prior,
        _numeric(item["absolute_change"], f"{label} delta", signed=True),
        cast("str | None", percentage),
        change_from_payload(
            item[CHANGE_KEY],
            ChangeOperands(current.value, prior.value, current.units),
            label,
        ),
    )


def retail_health_artifact_from_payload(value: object) -> RetailHealthArtifact:
    """Strictly decode and re-attest a Retail Health V2 payload.

    Every body carries `evidence_status`, null on an empty or insufficient body, and a change
    key on every comparison. The retained reads of format-4 comparisons without a change key
    and format-5 bodies without the coverage key retired in the integration change, and so did
    the read of a format-7 body (the eleven-tile roster, no fixed statuses, no product detail):
    the API, a full re-bake and the browser deploy together (owner revision, 2026-10-08).
    """
    item = _object(
        value,
        {
            EVIDENCE_STATUS_KEY,
            RETURN_REASONS_KEY,
            AMC_RETAIL_PURCHASES_KEY,
            "kind",
            "outcome",
            "period",
            "prior_period",
            "distributor_view",
            "totals",
            "absences",
            "insights",
            "trend",
            "rows",
            "total_count",
            "truncation",
            "reconciliation",
            "boundary",
        },
        "retail health artifact",
    )
    # Every displayed comparison value carries the key or none does; a mix is a writer bug.
    display_value_mode(_comparison_metric_values(item))
    try:
        outcome = RetailHealthOutcome(_text(item["outcome"], "retail health outcome"))
        distributor_view = RetailDistributorView(
            _text(item["distributor_view"], "retail distributor view")
        )
    except ValueError:
        raise ValueError("retail health enum is unsupported.") from None
    absences = tuple(
        RetailTypedAbsence(
            _text(
                _object(raw, {"metric", "reason"}, "retail absence")["metric"],
                "retail absence metric",
            ),
            _text(
                _object(raw, {"metric", "reason"}, "retail absence")["reason"],
                "retail absence reason",
            ),
        )
        for raw in _items(item["absences"], "retail absences")
    )
    trend = tuple(
        RetailTrendPoint(
            _date(
                _object(raw, {"date", "current", "prior"}, "retail trend point")["date"],
                "retail trend date",
            ),
            tuple(
                _point_value(entry, "retail trend current")
                for entry in _items(
                    _object(raw, {"date", "current", "prior"}, "retail trend point")["current"],
                    "retail trend current",
                )
            ),
            tuple(
                _point_value(entry, "retail trend prior")
                for entry in _items(
                    _object(raw, {"date", "current", "prior"}, "retail trend point")["prior"],
                    "retail trend prior",
                )
            ),
        )
        for raw in _items(item["trend"], "retail trend")
    )
    rows = tuple(_row(raw) for raw in _items(item["rows"], "retail rows"))
    check_retail_fixed_statuses(item, "retail health artifact")
    reconciliation_value = item["reconciliation"]
    reconciliation = None
    if reconciliation_value is not None:
        raw = _object(
            reconciliation_value,
            {
                "outcome",
                "as_of",
                "product_revenue",
                "reported_revenue",
                "product_units",
                "reported_units",
            },
            "retail reconciliation",
        )
        nullable = lambda key: (
            None if raw[key] is None else _numeric(raw[key], f"retail reconciliation {key}")
        )
        reconciliation = RetailReconciliation(
            _text(raw["outcome"], "retail reconciliation outcome"),
            _date(raw["as_of"], "retail reconciliation as of"),
            nullable("product_revenue"),
            nullable("reported_revenue"),
            nullable("product_units"),
            nullable("reported_units"),
        )
    count = item["total_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("retail total count is invalid.")
    truncation = item["truncation"]
    if truncation is not None and (isinstance(truncation, bool) or not isinstance(truncation, int)):
        raise ValueError("retail truncation is invalid.")
    artifact = RetailHealthArtifact(
        outcome,
        _period(item["period"], "retail period"),
        _period(item["prior_period"], "retail prior period"),
        distributor_view,
        tuple(
            _comparison(entry, "retail total") for entry in _items(item["totals"], "retail totals")
        ),
        absences,
        insight_slots_from_payload(item["insights"], expected=_RETAIL_INSIGHT_IDS),
        trend,
        rows,
        count,
        cast("int | None", truncation),
        reconciliation,
        _text(item["boundary"], "retail boundary"),
        _text(item["kind"], "retail kind"),
        evidence_status_from_payload(item[EVIDENCE_STATUS_KEY]),
    )
    # ADR-119 decision 4: the block is null only on an empty or insufficient body.
    if artifact.outcome is RetailHealthOutcome.COMPLETE and artifact.evidence_status is None:
        raise ValueError("a complete Retail Health body carries its coverage block.")
    return artifact


_ROW_FIELDS = frozenset(
    {
        "child_asin",
        "parent_asin",
        "title",
        "comparisons",
        "shipped_revenue_spark",
        "freshness_qualifiers",
    }
)


def _row(value: object) -> RetailProductRow:
    """Decode one product row; a served row also carries its opaque selection reference."""
    served = isinstance(value, dict) and "selection_reference" in value
    row = _object(
        value, {*_ROW_FIELDS, *(("selection_reference",) if served else ())}, "retail row"
    )
    return RetailProductRow(
        _text(row["child_asin"], "retail child ASIN"),
        _text(row["parent_asin"], "retail parent ASIN"),
        _text(row["title"], "retail product title"),
        tuple(
            _comparison(entry, "retail row comparison")
            for entry in _items(row["comparisons"], "retail row comparisons")
        ),
        tuple(
            _point_value(entry, "retail row spark")
            for entry in _items(row["shipped_revenue_spark"], "retail row spark")
        ),
        tuple(
            _text(entry, "retail row freshness")
            for entry in _items(row["freshness_qualifiers"], "retail row freshness")
        ),
        _text(row["selection_reference"], "retail row selection reference") if served else None,
    )


# Shared with the product detail child (ADR-125 decision 4): one value and comparison codec.
retail_comparison_payload = _comparison_payload
retail_comparison_from_payload = _comparison
retail_point_payload = _point_payload
retail_point_from_payload = _point_value
