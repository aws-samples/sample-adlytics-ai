# SPDX-License-Identifier: MIT-0
"""Pure marketplace comparison reduction and canonical public payload contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import cast

from domain.measurement.comparison import absolute_change, percentage_change
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import CURRENCY_PATTERN, DateRange, Grain

MARKETPLACE_COMPARISON_ARTIFACT_TYPE = "MarketplaceComparisonArtifact"
MARKETPLACE_COMPARISON_ARTIFACT_VERSION = 1
MARKETPLACE_GRAIN = Grain(("period", "marketplace"))
MARKETPLACE_CURRENCY_POLICY = "native_currency_only_no_fx_no_cross_market_magnitude"
MARKETPLACE_SOURCE_TIME_POLICY = "qualified_source_business_dates_no_temporal_alignment"
MARKETPLACE_COMPARISON_BOUNDARY = (
    "Fee-inclusive ad spend is ad-attributed and shipped retail revenue is retail-actual; "
    "direction agreement is descriptive and does not establish attribution or causality."
)
_MIN_COMPARABLE_MARKETPLACES = 2
_SOURCE_BUSINESS_DATE = "source_business_date"
MARKETPLACE_DECIMAL_MAX_COEFFICIENT_DIGITS = 256
MARKETPLACE_DECIMAL_MAX_ABS_EXPONENT = 10_000
_MARKETPLACE_DECIMAL_MAX_EXPONENT_DIGITS = len(str(MARKETPLACE_DECIMAL_MAX_ABS_EXPONENT))
_MARKETPLACE_DECIMAL_TEXT = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:E[+-](?:0|[1-9][0-9]*))?"
)


class MarketplaceComparisonState(StrEnum):
    """Closed top-level marketplace comparison states."""

    COMPARABLE = "comparable"
    ONE_MARKETPLACE = "one_marketplace"
    INSUFFICIENT = "insufficient"
    NO_COVERAGE = "no_coverage"


class MarketplaceMetric(StrEnum):
    """The only two parallel measurement bases admitted by V1."""

    AD_SPEND = "ad_spend"
    RETAIL_REVENUE_SHIPPED = "retail_revenue_shipped"


class MarketplacePeriodRole(StrEnum):
    """The two exact windows represented by one metric evidence record."""

    CURRENT = "current"
    COMPARATOR = "comparator"


class MarketplaceEvidenceCondition(StrEnum):
    """Closed exact condition for one metric and period side."""

    AVAILABLE = "available"
    MISSING_PUBLICATION = "missing_publication"
    MISSING = "missing"
    COVERAGE_HOLE = "coverage_hole"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    STALE = "stale"
    QUARANTINED = "quarantined"
    INCOMPATIBLE_SCOPE = "incompatible_scope"


class MarketplaceDirection(StrEnum):
    """Exact signed movement without a materiality threshold."""

    INCREASE = "increase"
    DECREASE = "decrease"
    FLAT = "flat"


class MarketplaceRelation(StrEnum):
    """Closed relation between the two parallel metric directions."""

    SAME_DIRECTION = "same_direction"
    OPPOSITE_DIRECTION = "opposite_direction"
    BOTH_FLAT = "both_flat"
    MIXED_FLAT = "mixed_flat"


class MarketplaceComparability(StrEnum):
    """Whether complete evidence supports one descriptive row relation."""

    COMPARABLE = "comparable"
    INVALID = "invalid"


class MarketplaceComparisonReason(StrEnum):
    """Closed invalidity reasons in their single deterministic precedence."""

    MISSING_CURRENT_PUBLICATION = "missing_current_publication"
    MISSING_COMPARATOR_PUBLICATION = "missing_comparator_publication"
    MISSING_CURRENT = "missing_current"
    MISSING_COMPARATOR = "missing_comparator"
    COVERAGE_HOLE = "coverage_hole"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    STALE = "stale"
    QUARANTINED = "quarantined"
    CURRENCY_CHANGED = "currency_changed"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    SOURCE_CALENDAR_MISMATCH = "source_calendar_mismatch"


MARKETPLACE_REASON_PRECEDENCE = tuple(MarketplaceComparisonReason)
_REASON_POSITION = {reason: index for index, reason in enumerate(MARKETPLACE_REASON_PRECEDENCE)}
_METRIC_ORDER = (MarketplaceMetric.AD_SPEND, MarketplaceMetric.RETAIL_REVENUE_SHIPPED)
_EXPECTED_BASIS = {
    MarketplaceMetric.AD_SPEND: MeasurementBasis.AD_ATTRIBUTED,
    MarketplaceMetric.RETAIL_REVENUE_SHIPPED: MeasurementBasis.RETAIL_ACTUAL,
}
_EXPECTED_SOURCE = {
    MarketplaceMetric.AD_SPEND: "amc",
    MarketplaceMetric.RETAIL_REVENUE_SHIPPED: "vendor_central",
}


def _validate_decimal_artifact(value: Decimal, label: str) -> None:
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if (
        len(digits) > MARKETPLACE_DECIMAL_MAX_COEFFICIENT_DIGITS
        or not isinstance(exponent, int)
        or abs(exponent) > MARKETPLACE_DECIMAL_MAX_ABS_EXPONENT
    ):
        raise ValueError(f"{label} exceeds the Marketplace artifact Decimal bound.")


def _nonblank(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} cannot be blank.")


def _ordered_ranges(values: tuple[DateRange, ...], label: str) -> None:
    if values != tuple(sorted(values, key=lambda item: (item.start, item.end))):
        raise ValueError(f"{label} must be chronologically ordered.")
    if any(left.overlaps(right) for left, right in pairwise(values)):
        raise ValueError(f"{label} cannot overlap.")


def _previous_period(current: DateRange) -> DateRange:
    end = current.start - timedelta(days=1)
    return DateRange(end - timedelta(days=current.days - 1), end)


def _ordered_reasons(
    reasons: set[MarketplaceComparisonReason],
) -> tuple[MarketplaceComparisonReason, ...]:
    return tuple(reason for reason in MARKETPLACE_REASON_PRECEDENCE if reason in reasons)


@dataclass(frozen=True, slots=True)
class MarketplaceSourceTime:
    """Exact source-owned business-day semantics; no rebucketing is implied."""

    source_timezone: str
    business_calendar: str
    observation_basis: str = _SOURCE_BUSINESS_DATE

    def __post_init__(self) -> None:
        _nonblank(self.source_timezone, "marketplace source timezone")
        _nonblank(self.business_calendar, "marketplace business calendar")
        if self.observation_basis != _SOURCE_BUSINESS_DATE:
            raise ValueError("marketplace observation basis must be source_business_date.")


@dataclass(frozen=True, slots=True)
class MarketplacePeriodEvidence:
    """One current or comparator monetary value with exact evidence semantics."""

    condition: MarketplaceEvidenceCondition
    freshness_status: FreshnessStatus | None
    requested_period: DateRange
    actual_coverage: tuple[DateRange, ...]
    coverage_holes: tuple[DateRange, ...]
    value: Decimal | None
    units: Units | None
    grain: Grain
    basis: MeasurementBasis
    source: str
    source_time: MarketplaceSourceTime
    suppression: Suppression
    row_count: int
    truncated: bool

    def __post_init__(self) -> None:
        _nonblank(self.source, "marketplace evidence source")
        if self.units is not None and (
            self.units.kind is not UnitKind.CURRENCY
            or self.units.currency_code is None
            or re.fullmatch(CURRENCY_PATTERN, self.units.currency_code) is None
        ):
            raise ValueError("marketplace comparison evidence must use ISO currency units.")
        if self.grain != MARKETPLACE_GRAIN:
            raise ValueError("marketplace comparison evidence must use period-marketplace grain.")
        if self.row_count < 0:
            raise ValueError("marketplace evidence row count cannot be negative.")
        if self.value is not None and (
            not isinstance(cast("object", self.value), Decimal) or not self.value.is_finite()
        ):
            raise ValueError("marketplace evidence values must be finite Decimals.")
        if self.value is not None:
            _validate_decimal_artifact(self.value, "marketplace evidence value")
        if self.value is not None and self.units is None:
            raise ValueError("represented marketplace evidence requires native currency units.")
        if (
            self.condition
            in {
                MarketplaceEvidenceCondition.MISSING_PUBLICATION,
                MarketplaceEvidenceCondition.MISSING,
            }
            and self.units is not None
        ):
            raise ValueError("unrepresented marketplace evidence cannot claim currency units.")
        _ordered_ranges(self.actual_coverage, "marketplace actual coverage")
        _ordered_ranges(self.coverage_holes, "marketplace coverage holes")
        if any(not self.requested_period.covers(item) for item in self.actual_coverage):
            raise ValueError("marketplace actual coverage must fall inside the requested period.")
        if any(not self.requested_period.covers(item) for item in self.coverage_holes):
            raise ValueError("marketplace coverage holes must fall inside the requested period.")
        self._validate_condition()

    def _validate_condition(self) -> None:
        complete = self.actual_coverage == (self.requested_period,) and not self.coverage_holes
        if self.freshness_status is None:
            if (
                self.actual_coverage
                or self.coverage_holes
                or self.value is not None
                or self.row_count != 0
                or self.suppression.rows_suppressed
                or self.truncated
            ):
                raise ValueError(
                    "missing publication evidence cannot claim freshness, coverage, rows, or value."
                )
        elif self.freshness_status not in {
            FreshnessStatus.FRESH,
            FreshnessStatus.STALE,
            FreshnessStatus.QUARANTINED,
        }:
            raise ValueError("present marketplace evidence requires exact inspected freshness.")
        if self.value is None and self.units is not None:
            raise ValueError("unrepresented marketplace evidence cannot claim currency units.")
        if self.suppression.rows_suppressed and self.value is not None:
            raise ValueError("suppressed marketplace evidence cannot claim a represented value.")

        expected = self._derived_condition(complete)
        if self.condition is not expected:
            raise ValueError("marketplace evidence condition contradicts its exact signals.")

    def _derived_condition(self, complete: bool) -> MarketplaceEvidenceCondition:
        condition: MarketplaceEvidenceCondition
        if self.freshness_status is None:
            condition = MarketplaceEvidenceCondition.MISSING_PUBLICATION
        elif self.coverage_holes:
            condition = MarketplaceEvidenceCondition.COVERAGE_HOLE
        elif self.suppression.rows_suppressed:
            condition = MarketplaceEvidenceCondition.SUPPRESSED
        elif self.truncated:
            condition = MarketplaceEvidenceCondition.TRUNCATED
        elif self.freshness_status is FreshnessStatus.STALE:
            condition = MarketplaceEvidenceCondition.STALE
        elif self.freshness_status is FreshnessStatus.QUARANTINED:
            condition = MarketplaceEvidenceCondition.QUARANTINED
        elif not complete:
            condition = MarketplaceEvidenceCondition.INCOMPATIBLE_SCOPE
        elif self.value is not None:
            if self.units is None or self.row_count != 1:
                raise ValueError(
                    "represented marketplace evidence requires one native-currency row."
                )
            condition = MarketplaceEvidenceCondition.AVAILABLE
        else:
            if self.row_count != 0:
                raise ValueError("missing marketplace row cannot claim represented rows.")
            condition = MarketplaceEvidenceCondition.MISSING
        return condition

    @property
    def available(self) -> bool:
        """Return whether this exact side is usable, preserving represented zero."""
        return self.condition is MarketplaceEvidenceCondition.AVAILABLE


@dataclass(frozen=True, slots=True)
class MarketplaceMetricEvidence:
    """Current and comparator evidence for one metric at one canonical marketplace."""

    marketplace_key: str
    metric: MarketplaceMetric
    current: MarketplacePeriodEvidence
    comparator: MarketplacePeriodEvidence

    def __post_init__(self) -> None:
        _nonblank(self.marketplace_key, "canonical marketplace key")


@dataclass(frozen=True, slots=True)
class MarketplaceComparisonRequest:
    """All exact metric evidence and identity-free absent-slot reasons for the reducer."""

    current_window: DateRange
    comparison_window: DateRange
    evidence: tuple[MarketplaceMetricEvidence, ...]
    publication_reasons: tuple[MarketplaceComparisonReason, ...] = ()

    def __post_init__(self) -> None:
        if self.comparison_window != _previous_period(self.current_window):
            raise ValueError(
                "marketplace comparison requires the immediately preceding equal-length period."
            )
        if self.publication_reasons != _ordered_reasons(set(self.publication_reasons)) or any(
            reason
            not in {
                MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION,
                MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION,
            }
            for reason in self.publication_reasons
        ):
            raise ValueError("marketplace publication reasons must be canonical slot reasons.")
        identities = tuple((item.marketplace_key, item.metric) for item in self.evidence)
        if len(set(identities)) != len(identities):
            raise ValueError("marketplace comparison evidence repeats a marketplace metric.")
        for item in self.evidence:
            if item.current.requested_period != self.current_window:
                raise ValueError("current marketplace evidence uses the wrong requested period.")
            if item.comparator.requested_period != self.comparison_window:
                raise ValueError("comparator marketplace evidence uses the wrong requested period.")


@dataclass(frozen=True, slots=True)
class MarketplaceMetricMovement:
    """Backend-derived exact movement for one metric without cross-basis arithmetic."""

    metric: MarketplaceMetric
    current: MarketplacePeriodEvidence
    comparator: MarketplacePeriodEvidence
    absolute_change: Decimal | None
    percentage_change: str | None
    direction: MarketplaceDirection | None
    comparability: MarketplaceComparability
    reasons: tuple[MarketplaceComparisonReason, ...]

    def __post_init__(self) -> None:
        if self.absolute_change is not None:
            _validate_decimal_artifact(self.absolute_change, "marketplace absolute change")
        if self.reasons != _ordered_reasons(set(self.reasons)) or len(set(self.reasons)) != len(
            self.reasons
        ):
            raise ValueError("marketplace movement reasons must be unique and canonical.")
        expected = _derive_movement(self.metric, self.current, self.comparator)
        comparable, change, percent, direction, reasons = expected
        if (
            self.comparability is not comparable
            or self.absolute_change != change
            or self.percentage_change != percent
            or self.direction is not direction
            or self.reasons != reasons
        ):
            raise ValueError("marketplace movement must be derived from exact evidence.")


@dataclass(frozen=True, slots=True)
class MarketplaceComparisonRow:
    """Canonical-keyed joined metric row; public labels are applied only at projection."""

    marketplace_key: str
    ad_spend: MarketplaceMetricMovement
    retail_revenue_shipped: MarketplaceMetricMovement
    relation: MarketplaceRelation | None
    comparability: MarketplaceComparability
    reasons: tuple[MarketplaceComparisonReason, ...]

    def __post_init__(self) -> None:
        _nonblank(self.marketplace_key, "canonical marketplace key")
        if self.ad_spend.metric is not MarketplaceMetric.AD_SPEND or (
            self.retail_revenue_shipped.metric is not MarketplaceMetric.RETAIL_REVENUE_SHIPPED
        ):
            raise ValueError("marketplace row metrics must preserve the registered order.")
        expected_comparable = (
            self.ad_spend.comparability is MarketplaceComparability.COMPARABLE
            and self.retail_revenue_shipped.comparability is MarketplaceComparability.COMPARABLE
        )
        expected_verdict = (
            MarketplaceComparability.COMPARABLE
            if expected_comparable
            else MarketplaceComparability.INVALID
        )
        expected_reasons = _ordered_reasons(
            set(self.ad_spend.reasons) | set(self.retail_revenue_shipped.reasons)
        )
        expected_relation = (
            _relation(self.ad_spend.direction, self.retail_revenue_shipped.direction)
            if expected_comparable
            else None
        )
        if (
            self.comparability is not expected_verdict
            or self.reasons != expected_reasons
            or self.relation is not expected_relation
        ):
            raise ValueError("marketplace row verdict and relation must be server-derived.")


@dataclass(frozen=True, slots=True)
class MarketplaceComparisonArtifact:
    """Immutable canonical marketplace comparison with fixed V1 policy."""

    state: MarketplaceComparisonState
    current_window: DateRange
    comparison_window: DateRange
    rows: tuple[MarketplaceComparisonRow, ...]
    publication_reasons: tuple[MarketplaceComparisonReason, ...] = ()
    currency_policy: str = MARKETPLACE_CURRENCY_POLICY
    source_time_policy: str = MARKETPLACE_SOURCE_TIME_POLICY
    boundary: str = MARKETPLACE_COMPARISON_BOUNDARY

    def __post_init__(self) -> None:
        if self.comparison_window != _previous_period(self.current_window):
            raise ValueError("marketplace artifact requires the previous equal-length period.")
        if self.currency_policy != MARKETPLACE_CURRENCY_POLICY:
            raise ValueError("marketplace currency policy is fixed domain policy.")
        if self.source_time_policy != MARKETPLACE_SOURCE_TIME_POLICY:
            raise ValueError("marketplace source-time policy is fixed domain policy.")
        if self.boundary != MARKETPLACE_COMPARISON_BOUNDARY:
            raise ValueError("marketplace comparison boundary is fixed domain policy.")
        if self.publication_reasons != _ordered_reasons(set(self.publication_reasons)) or any(
            reason
            not in {
                MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION,
                MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION,
            }
            for reason in self.publication_reasons
        ):
            raise ValueError("marketplace publication reasons must be canonical slot reasons.")
        keys = tuple(row.marketplace_key for row in self.rows)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("marketplace artifact rows require unique canonical key order.")
        for row in self.rows:
            for movement in (row.ad_spend, row.retail_revenue_shipped):
                if movement.current.requested_period != self.current_window or (
                    movement.comparator.requested_period != self.comparison_window
                ):
                    raise ValueError("marketplace artifact evidence contradicts its periods.")
        if self.state is not reduce_marketplace_comparison_state(self.rows):
            raise ValueError("marketplace comparison state contradicts its rows.")


def _evidence_reasons(
    evidence: MarketplacePeriodEvidence,
    role: MarketplacePeriodRole,
) -> set[MarketplaceComparisonReason]:
    reasons: set[MarketplaceComparisonReason] = set()
    if evidence.condition is MarketplaceEvidenceCondition.MISSING_PUBLICATION:
        reasons.add(
            MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION
            if role is MarketplacePeriodRole.CURRENT
            else MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION
        )
    elif evidence.condition is MarketplaceEvidenceCondition.MISSING:
        reasons.add(
            MarketplaceComparisonReason.MISSING_CURRENT
            if role is MarketplacePeriodRole.CURRENT
            else MarketplaceComparisonReason.MISSING_COMPARATOR
        )
    if evidence.coverage_holes:
        reasons.add(MarketplaceComparisonReason.COVERAGE_HOLE)
    if evidence.suppression.rows_suppressed:
        reasons.add(MarketplaceComparisonReason.SUPPRESSED)
    if evidence.truncated:
        reasons.add(MarketplaceComparisonReason.TRUNCATED)
    if evidence.freshness_status is FreshnessStatus.STALE:
        reasons.add(MarketplaceComparisonReason.STALE)
    elif evidence.freshness_status is FreshnessStatus.QUARANTINED:
        reasons.add(MarketplaceComparisonReason.QUARANTINED)
    if evidence.freshness_status is not None and evidence.actual_coverage != (
        evidence.requested_period,
    ):
        reasons.add(MarketplaceComparisonReason.INCOMPATIBLE_SCOPE)
    return reasons


def _source_scope_valid(metric: MarketplaceMetric, evidence: MarketplacePeriodEvidence) -> bool:
    return (
        evidence.basis is _EXPECTED_BASIS[metric]
        and evidence.source == _EXPECTED_SOURCE[metric]
        and (evidence.units is None or evidence.units.kind is UnitKind.CURRENCY)
        and evidence.grain == MARKETPLACE_GRAIN
    )


def _derive_movement(
    metric: MarketplaceMetric,
    current: MarketplacePeriodEvidence,
    comparator: MarketplacePeriodEvidence,
) -> tuple[
    MarketplaceComparability,
    Decimal | None,
    str | None,
    MarketplaceDirection | None,
    tuple[MarketplaceComparisonReason, ...],
]:
    reasons = _evidence_reasons(current, MarketplacePeriodRole.CURRENT) | _evidence_reasons(
        comparator, MarketplacePeriodRole.COMPARATOR
    )
    if not _source_scope_valid(metric, current) or not _source_scope_valid(metric, comparator):
        reasons.add(MarketplaceComparisonReason.INCOMPATIBLE_SCOPE)
    if (
        current.units is not None
        and comparator.units is not None
        and current.units.currency_code != comparator.units.currency_code
    ):
        reasons.add(MarketplaceComparisonReason.CURRENCY_CHANGED)
    if current.source_time != comparator.source_time:
        reasons.add(MarketplaceComparisonReason.SOURCE_CALENDAR_MISMATCH)
    ordered = _ordered_reasons(reasons)
    if ordered:
        return MarketplaceComparability.INVALID, None, None, None, ordered
    if current.value is None or comparator.value is None:
        raise ValueError("comparable marketplace evidence requires both represented values.")
    change = cast("Decimal", absolute_change(current.value, comparator.value))
    direction = (
        MarketplaceDirection.INCREASE
        if change > 0
        else MarketplaceDirection.DECREASE
        if change < 0
        else MarketplaceDirection.FLAT
    )
    return (
        MarketplaceComparability.COMPARABLE,
        change,
        percentage_change(current.value, comparator.value),
        direction,
        (),
    )


def _movement(evidence: MarketplaceMetricEvidence) -> MarketplaceMetricMovement:
    comparable, change, percent, direction, reasons = _derive_movement(
        evidence.metric, evidence.current, evidence.comparator
    )
    return MarketplaceMetricMovement(
        metric=evidence.metric,
        current=evidence.current,
        comparator=evidence.comparator,
        absolute_change=change,
        percentage_change=percent,
        direction=direction,
        comparability=comparable,
        reasons=reasons,
    )


def _relation(
    ad_direction: MarketplaceDirection | None,
    retail_direction: MarketplaceDirection | None,
) -> MarketplaceRelation:
    if ad_direction is None or retail_direction is None:
        raise ValueError("marketplace relation requires two valid directions.")
    if ad_direction is MarketplaceDirection.FLAT and retail_direction is MarketplaceDirection.FLAT:
        return MarketplaceRelation.BOTH_FLAT
    if MarketplaceDirection.FLAT in {ad_direction, retail_direction}:
        return MarketplaceRelation.MIXED_FLAT
    if ad_direction is retail_direction:
        return MarketplaceRelation.SAME_DIRECTION
    return MarketplaceRelation.OPPOSITE_DIRECTION


def reduce_marketplace_comparison_state(
    rows: tuple[MarketplaceComparisonRow, ...],
) -> MarketplaceComparisonState:
    """Apply no-coverage precedence, then count complete paired marketplace rows."""
    current_available = any(
        movement.current.condition
        not in {
            MarketplaceEvidenceCondition.MISSING_PUBLICATION,
            MarketplaceEvidenceCondition.MISSING,
        }
        for row in rows
        for movement in (row.ad_spend, row.retail_revenue_shipped)
    )
    if not current_available:
        return MarketplaceComparisonState.NO_COVERAGE
    comparable = sum(row.comparability is MarketplaceComparability.COMPARABLE for row in rows)
    if comparable >= _MIN_COMPARABLE_MARKETPLACES:
        return MarketplaceComparisonState.COMPARABLE
    if comparable == 1:
        return MarketplaceComparisonState.ONE_MARKETPLACE
    return MarketplaceComparisonState.INSUFFICIENT


def build_marketplace_comparison(
    request: MarketplaceComparisonRequest,
) -> MarketplaceComparisonArtifact:
    """Join registered metrics by canonical marketplace identity and derive exact movement."""
    indexed = {(item.marketplace_key, item.metric): item for item in request.evidence}
    keys = tuple(sorted({item.marketplace_key for item in request.evidence}))
    rows: list[MarketplaceComparisonRow] = []
    for key in keys:
        missing = [metric for metric in _METRIC_ORDER if (key, metric) not in indexed]
        if missing:
            raise ValueError("every marketplace requires both registered metric evidence records.")
        ad = _movement(indexed[(key, MarketplaceMetric.AD_SPEND)])
        retail = _movement(indexed[(key, MarketplaceMetric.RETAIL_REVENUE_SHIPPED)])
        comparable = (
            ad.comparability is MarketplaceComparability.COMPARABLE
            and retail.comparability is MarketplaceComparability.COMPARABLE
        )
        rows.append(
            MarketplaceComparisonRow(
                marketplace_key=key,
                ad_spend=ad,
                retail_revenue_shipped=retail,
                relation=_relation(ad.direction, retail.direction) if comparable else None,
                comparability=(
                    MarketplaceComparability.COMPARABLE
                    if comparable
                    else MarketplaceComparability.INVALID
                ),
                reasons=_ordered_reasons(set(ad.reasons) | set(retail.reasons)),
            )
        )
    result = tuple(rows)
    return MarketplaceComparisonArtifact(
        state=reduce_marketplace_comparison_state(result),
        current_window=request.current_window,
        comparison_window=request.comparison_window,
        rows=result,
        publication_reasons=request.publication_reasons,
    )


_PERIOD_FIELDS = frozenset({"end", "start"})
_SOURCE_TIME_FIELDS = frozenset({"business_calendar", "observation_basis", "source_timezone"})
_UNITS_FIELDS = frozenset({"currency_code", "kind"})
_SUPPRESSION_FIELDS = frozenset({"reason", "rows_suppressed"})
_EVIDENCE_FIELDS = frozenset(
    {
        "actual_coverage",
        "basis",
        "condition",
        "coverage_holes",
        "freshness_status",
        "grain",
        "requested_period",
        "row_count",
        "source",
        "source_time",
        "suppression",
        "truncated",
        "units",
        "value",
    }
)
_MOVEMENT_FIELDS = frozenset(
    {
        "absolute_change",
        "comparability",
        "comparator",
        "current",
        "direction",
        "metric",
        "percentage_change",
        "reasons",
    }
)
_ROW_FIELDS = frozenset(
    {"ad_spend", "comparability", "marketplace", "reasons", "relation", "retail_revenue_shipped"}
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "boundary",
        "comparison_window",
        "currency_policy",
        "current_window",
        "publication_reasons",
        "rows",
        "source_time_policy",
        "state",
    }
)


def _period_payload(period: DateRange) -> dict[str, str]:
    return {"end": period.end.isoformat(), "start": period.start.isoformat()}


def _evidence_payload(evidence: MarketplacePeriodEvidence) -> dict[str, object]:
    return {
        "actual_coverage": [_period_payload(item) for item in evidence.actual_coverage],
        "basis": evidence.basis.value,
        "condition": evidence.condition.value,
        "coverage_holes": [_period_payload(item) for item in evidence.coverage_holes],
        "freshness_status": (
            None if evidence.freshness_status is None else evidence.freshness_status.value
        ),
        "grain": list(evidence.grain.dimensions),
        "requested_period": _period_payload(evidence.requested_period),
        "row_count": evidence.row_count,
        "source": evidence.source,
        "source_time": {
            "business_calendar": evidence.source_time.business_calendar,
            "observation_basis": evidence.source_time.observation_basis,
            "source_timezone": evidence.source_time.source_timezone,
        },
        "suppression": {
            "reason": evidence.suppression.reason,
            "rows_suppressed": evidence.suppression.rows_suppressed,
        },
        "truncated": evidence.truncated,
        "units": (
            None
            if evidence.units is None
            else {
                "currency_code": evidence.units.currency_code,
                "kind": evidence.units.kind.value,
            }
        ),
        "value": None if evidence.value is None else str(evidence.value),
    }


def _movement_payload(movement: MarketplaceMetricMovement) -> dict[str, object]:
    return {
        "absolute_change": (
            None if movement.absolute_change is None else str(movement.absolute_change)
        ),
        "comparability": movement.comparability.value,
        "comparator": _evidence_payload(movement.comparator),
        "current": _evidence_payload(movement.current),
        "direction": None if movement.direction is None else movement.direction.value,
        "metric": movement.metric.value,
        "percentage_change": movement.percentage_change,
        "reasons": [reason.value for reason in movement.reasons],
    }


def _label_index(
    labels: tuple[PresentationDimensionLabel, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    relevant = tuple(label for label in labels if label.selector_field == "marketplace_key")
    if len(relevant) != len(labels):
        raise ValueError("marketplace comparison accepts only governed marketplace labels.")
    by_key = {label.selector_value: label.display_value for label in relevant}
    by_display = {label.display_value: label.selector_value for label in relevant}
    if len(by_key) != len(relevant) or len(by_display) != len(relevant):
        raise ValueError("marketplace labels must have unique selector and display identities.")
    return by_key, by_display


def marketplace_comparison_payload(
    artifact: MarketplaceComparisonArtifact,
    labels: tuple[PresentationDimensionLabel, ...],
) -> dict[str, object]:
    """Project canonical keys to governed labels in the strict browser-safe V1 payload."""
    by_key, _by_display = _label_index(labels)
    missing = {row.marketplace_key for row in artifact.rows} - set(by_key)
    if missing:
        raise ValueError("marketplace comparison artifact lacks a governed public label.")
    return {
        "artifact_type": MARKETPLACE_COMPARISON_ARTIFACT_TYPE,
        "artifact_version": MARKETPLACE_COMPARISON_ARTIFACT_VERSION,
        "boundary": artifact.boundary,
        "comparison_window": _period_payload(artifact.comparison_window),
        "currency_policy": artifact.currency_policy,
        "current_window": _period_payload(artifact.current_window),
        "publication_reasons": [reason.value for reason in artifact.publication_reasons],
        "rows": [
            {
                "ad_spend": _movement_payload(row.ad_spend),
                "comparability": row.comparability.value,
                "marketplace": by_key[row.marketplace_key],
                "reasons": [reason.value for reason in row.reasons],
                "relation": None if row.relation is None else row.relation.value,
                "retail_revenue_shipped": _movement_payload(row.retail_revenue_shipped),
            }
            for row in artifact.rows
        ],
        "source_time_policy": artifact.source_time_policy,
        "state": artifact.state.value,
    }


def _object(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or frozenset(cast("dict[object, object]", value).keys()) != fields
    ):
        raise ValueError(f"{label} fields are not exact.")
    return cast("dict[str, object]", value)


def _items(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return cast("list[object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean.")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    return value


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be an ISO date.") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be a canonical ISO date.")
    return parsed


def _period(value: object, label: str) -> DateRange:
    item = _object(value, _PERIOD_FIELDS, label)
    return DateRange(_date(item["start"], f"{label} start"), _date(item["end"], f"{label} end"))


def _periods(value: object, label: str) -> tuple[DateRange, ...]:
    return tuple(_period(item, label) for item in _items(value, label))


def _decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or _MARKETPLACE_DECIMAL_TEXT.fullmatch(value) is None:
        raise ValueError(f"{label} must be canonical Decimal text or null.")
    unsigned = value.removeprefix("-")
    mantissa, separator, exponent_text = unsigned.partition("E")
    whole, point, fraction = mantissa.partition(".")
    coefficient_digits = f"{whole}{fraction}"
    if len(coefficient_digits) > MARKETPLACE_DECIMAL_MAX_COEFFICIENT_DIGITS:
        raise ValueError(f"{label} Decimal coefficient exceeds the artifact bound.")
    if separator and len(exponent_text.lstrip("+-")) > _MARKETPLACE_DECIMAL_MAX_EXPONENT_DIGITS:
        raise ValueError(f"{label} Decimal exponent exceeds the artifact bound.")
    explicit_exponent = int(exponent_text) if separator else 0
    exponent = explicit_exponent - (len(fraction) if point else 0)
    if abs(exponent) > MARKETPLACE_DECIMAL_MAX_ABS_EXPONENT:
        raise ValueError(f"{label} Decimal exponent exceeds the artifact bound.")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{label} Decimal text is invalid.") from None
    if not result.is_finite() or str(result) != value:
        raise ValueError(f"{label} Decimal text is not canonical.")
    return result


def _enum(enum_type: type[StrEnum], value: object, label: str) -> StrEnum:
    try:
        return enum_type(_text(value, label))
    except ValueError:
        raise ValueError(f"{label} is unsupported.") from None


def _evidence_from_payload(value: object, label: str) -> MarketplacePeriodEvidence:
    item = _object(value, _EVIDENCE_FIELDS, label)
    units_item = (
        None if item["units"] is None else _object(item["units"], _UNITS_FIELDS, f"{label} units")
    )
    source_time_item = _object(item["source_time"], _SOURCE_TIME_FIELDS, f"{label} source time")
    suppression_item = _object(item["suppression"], _SUPPRESSION_FIELDS, f"{label} suppression")
    dimensions = tuple(
        _text(dimension, f"{label} grain") for dimension in _items(item["grain"], f"{label} grain")
    )
    evidence = MarketplacePeriodEvidence(
        condition=cast(
            "MarketplaceEvidenceCondition",
            _enum(MarketplaceEvidenceCondition, item["condition"], f"{label} condition"),
        ),
        requested_period=_period(item["requested_period"], f"{label} requested period"),
        actual_coverage=_periods(item["actual_coverage"], f"{label} actual coverage"),
        coverage_holes=_periods(item["coverage_holes"], f"{label} coverage holes"),
        freshness_status=(
            None
            if item["freshness_status"] is None
            else cast(
                "FreshnessStatus",
                _enum(FreshnessStatus, item["freshness_status"], f"{label} freshness status"),
            )
        ),
        value=_decimal(item["value"], f"{label} value"),
        units=(
            None
            if units_item is None
            else Units(
                cast("UnitKind", _enum(UnitKind, units_item["kind"], f"{label} unit kind")),
                _optional_text(units_item["currency_code"], f"{label} currency"),
            )
        ),
        grain=Grain(dimensions),
        basis=cast(
            "MeasurementBasis",
            _enum(MeasurementBasis, item["basis"], f"{label} basis"),
        ),
        source=_text(item["source"], f"{label} source"),
        source_time=MarketplaceSourceTime(
            source_timezone=_text(source_time_item["source_timezone"], f"{label} source timezone"),
            business_calendar=_text(
                source_time_item["business_calendar"], f"{label} business calendar"
            ),
            observation_basis=_text(
                source_time_item["observation_basis"], f"{label} observation basis"
            ),
        ),
        suppression=Suppression(
            _boolean(suppression_item["rows_suppressed"], f"{label} rows suppressed"),
            _optional_text(suppression_item["reason"], f"{label} suppression reason"),
        ),
        row_count=_integer(item["row_count"], f"{label} row count"),
        truncated=_boolean(item["truncated"], f"{label} truncated"),
    )
    if _evidence_payload(evidence) != item:
        raise ValueError(f"{label} is not canonical.")
    return evidence


def _movement_from_payload(value: object, label: str) -> MarketplaceMetricMovement:
    item = _object(value, _MOVEMENT_FIELDS, label)
    try:
        metric = MarketplaceMetric(_text(item["metric"], f"{label} metric"))
        comparability = MarketplaceComparability(
            _text(item["comparability"], f"{label} comparability")
        )
        direction = (
            None
            if item["direction"] is None
            else MarketplaceDirection(_text(item["direction"], f"{label} direction"))
        )
        reasons = tuple(
            MarketplaceComparisonReason(_text(reason, f"{label} reason"))
            for reason in _items(item["reasons"], f"{label} reasons")
        )
    except ValueError:
        raise ValueError(f"{label} enum value is unsupported.") from None
    movement = MarketplaceMetricMovement(
        metric=metric,
        current=_evidence_from_payload(item["current"], f"{label} current"),
        comparator=_evidence_from_payload(item["comparator"], f"{label} comparator"),
        absolute_change=_decimal(item["absolute_change"], f"{label} absolute change"),
        percentage_change=_optional_text(item["percentage_change"], f"{label} percentage"),
        direction=direction,
        comparability=comparability,
        reasons=reasons,
    )
    if _movement_payload(movement) != item:
        raise ValueError(f"{label} is not canonical.")
    return movement


def marketplace_comparison_from_payload(
    value: object,
    labels: tuple[PresentationDimensionLabel, ...],
) -> MarketplaceComparisonArtifact:
    """Decode, reconstruct canonical keys, and re-attest the exact public V1 payload."""
    item = _object(value, _ARTIFACT_FIELDS, "marketplace comparison artifact")
    if _text(item["artifact_type"], "marketplace artifact type") != (
        MARKETPLACE_COMPARISON_ARTIFACT_TYPE
    ) or _integer(item["artifact_version"], "marketplace artifact version") != (
        MARKETPLACE_COMPARISON_ARTIFACT_VERSION
    ):
        raise ValueError("marketplace comparison artifact identity is unsupported.")
    _by_key, by_display = _label_index(labels)
    rows: list[MarketplaceComparisonRow] = []
    for raw_row in _items(item["rows"], "marketplace rows"):
        row = _object(raw_row, _ROW_FIELDS, "marketplace row")
        display = _text(row["marketplace"], "marketplace display label")
        key = by_display.get(display)
        if key is None:
            raise ValueError("marketplace payload label is not governed.")
        try:
            relation = (
                None
                if row["relation"] is None
                else MarketplaceRelation(_text(row["relation"], "marketplace relation"))
            )
            comparability = MarketplaceComparability(
                _text(row["comparability"], "marketplace comparability")
            )
            reasons = tuple(
                MarketplaceComparisonReason(_text(reason, "marketplace reason"))
                for reason in _items(row["reasons"], "marketplace reasons")
            )
        except ValueError:
            raise ValueError("marketplace row enum value is unsupported.") from None
        rows.append(
            MarketplaceComparisonRow(
                marketplace_key=key,
                ad_spend=_movement_from_payload(row["ad_spend"], "marketplace ad spend"),
                retail_revenue_shipped=_movement_from_payload(
                    row["retail_revenue_shipped"], "marketplace shipped retail revenue"
                ),
                relation=relation,
                comparability=comparability,
                reasons=reasons,
            )
        )
    try:
        state = MarketplaceComparisonState(_text(item["state"], "marketplace state"))
    except ValueError:
        raise ValueError("marketplace state is unsupported.") from None
    artifact = MarketplaceComparisonArtifact(
        state=state,
        current_window=_period(item["current_window"], "marketplace current window"),
        comparison_window=_period(item["comparison_window"], "marketplace comparison window"),
        rows=tuple(rows),
        publication_reasons=tuple(
            cast(
                "MarketplaceComparisonReason",
                _enum(MarketplaceComparisonReason, reason, "marketplace publication reason"),
            )
            for reason in _items(item["publication_reasons"], "marketplace publication reasons")
        ),
        currency_policy=_text(item["currency_policy"], "marketplace currency policy"),
        source_time_policy=_text(item["source_time_policy"], "marketplace source-time policy"),
        boundary=_text(item["boundary"], "marketplace boundary"),
    )
    if marketplace_comparison_payload(artifact, labels) != item:
        raise ValueError("marketplace comparison payload is not canonical.")
    return artifact


# Explicit aliases for callers using the document's comparator vocabulary.
MarketplaceComparisonMetric = MarketplaceMetric
MarketplaceComparisonDirection = MarketplaceDirection
MarketplaceComparisonRelation = MarketplaceRelation
build_marketplace_comparison_artifact = build_marketplace_comparison
