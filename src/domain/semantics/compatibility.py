# SPDX-License-Identifier: MIT-0
"""Typed compatibility and axis-aware aggregation for Semantic Architecture V2.

ADR-048. A note saying “never sum this across time” cannot constrain a planner. These values compile
that sentence into behavior: every declared axis has an aggregation rule, algebra and axis behavior
must agree, and every permitted composition difference carries a mandatory structured qualifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from domain.semantics.identity import CommerceScopeKey
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, TimeGrain
from domain.shared_kernel.provenance import MeasurementBasis

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Mapping

__all__ = [
    "DISJOINT_AXES",
    "NETWORK_AXES",
    "AggregationKind",
    "AggregationOperation",
    "AggregationPolicy",
    "AxisBehavior",
    "AxisPolicy",
    "CompatibilityDifference",
    "CompatibilityField",
    "CompatibilityResult",
    "CompatibilitySignature",
    "CompositionOperation",
    "CompositionRule",
    "RatioOfAggregatesPolicy",
    "RatioOfSumsPolicy",
    "SemanticAxis",
    "SemanticCompatibilityError",
    "compare_signatures",
    "validate_composition",
]


class SemanticCompatibilityError(ValueError):
    """A requested aggregation or composition would erase a semantic difference."""


class SemanticAxis(StrEnum):
    """Governed axes across which a metric may be grouped, filtered or aggregated."""

    TIME = "time"
    ECOSYSTEM = "ecosystem"
    PLATFORM = "platform"
    CHANNEL_GROUP = "channel_group"
    CHANNEL = "channel"
    COMMERCE_SCOPE = "commerce_scope"
    MARKETPLACE = "marketplace"
    PRODUCT = "product"
    CAMPAIGN = "campaign"
    AD_PRODUCT = "ad_product"
    ATTRIBUTION_MODEL = "attribution_model"
    SOURCE_OPTION = "source_option"
    MEASUREMENT_BASIS = "measurement_basis"
    EXPOSURE_GROUP = "exposure_group"
    FREQUENCY_BUCKET = "frequency_bucket"
    AUDIENCE_SEGMENT = "audience_segment"
    """Overlapping audience segments (ADR-103): exact only, never summed (D-4)."""
    IDENTITY_SPACE = "identity_space"
    ACQUISITION_CHANNEL_GROUP = "acquisition_channel_group"
    COHORT = "cohort"
    """A cohort of one vintage (ADR-104): the calendar month (or quarter) a shopper was won in."""
    MONTHS_SINCE_ACQUISITION = "months_since_acquisition"
    """A cohort's follow-up month k (or quarter): overlapping, never summed (design §6.6)."""


DISJOINT_AXES = frozenset(
    {
        SemanticAxis.EXPOSURE_GROUP,
        SemanticAxis.FREQUENCY_BUCKET,
        SemanticAxis.IDENTITY_SPACE,
        SemanticAxis.ACQUISITION_CHANNEL_GROUP,
    }
)
"""The shopper-aggregate axes that put each shopper in exactly one value (ADR-101; design D-3).

Only these may carry `disjoint_within_extraction`. No fact has one of these columns yet; the
first shopper-aggregate fact maps them.
"""


class AggregationKind(StrEnum):
    """Algebra a measure uses when rows are combined."""

    SUM = "sum"
    LAST = "last"
    MIN = "min"
    MAX = "max"
    EXACT_DISTINCT = "exact_distinct"
    MERGE_SKETCH = "merge_sketch"
    RATIO_OF_SUMS = "ratio_of_sums"
    NON_AGGREGATABLE = "non_aggregatable"


class AggregationOperation(StrEnum):
    """Operation requested across an omitted axis."""

    SUM = "sum"
    LAST = "last"
    MIN = "min"
    MAX = "max"
    MERGE = "merge"


class AxisBehavior(StrEnum):
    """What may happen when an axis is absent from the result grain."""

    ADDITIVE = "additive"
    LAST_VALUE = "last_value"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    MERGEABLE = "mergeable"
    EXACT_ONLY = "exact_only"
    FORBIDDEN = "forbidden"
    DISJOINT_WITHIN_EXTRACTION = "disjoint_within_extraction"
    """Summable across every value of a disjoint axis, inside one delivery (design D-3, D-9).

    Exact by construction, because each shopper falls in exactly one value. Allowed only on
    `DISJOINT_AXES`, and only where every other axis is `exact_only`, so no sum can leave one
    delivery. That every value came back is a property of the rows, checked when they are summed
    (`domain.shopper_aggregates.sum_disjoint`).
    """

    def permits(self, operation: AggregationOperation) -> bool:
        """Whether this axis behavior supports the requested deterministic operation."""
        return {
            AxisBehavior.ADDITIVE: AggregationOperation.SUM,
            AxisBehavior.LAST_VALUE: AggregationOperation.LAST,
            AxisBehavior.MINIMUM: AggregationOperation.MIN,
            AxisBehavior.MAXIMUM: AggregationOperation.MAX,
            AxisBehavior.MERGEABLE: AggregationOperation.MERGE,
            AxisBehavior.DISJOINT_WITHIN_EXTRACTION: AggregationOperation.SUM,
        }.get(self) is operation


@dataclass(frozen=True, slots=True)
class AxisPolicy:
    """Complete executable capabilities for one available semantic axis.

    Grouping, filtering and alignment are distinct from aggregation. A metric may be filterable by a
    reporting lens and forbidden to aggregate across it; two facts may expose an axis and still be
    forbidden to align on it. Every boolean is required so absence cannot mean permission.
    """

    aggregation: AxisBehavior
    groupable: bool
    filterable: bool
    alignable: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("groupable", self.groupable),
            ("filterable", self.filterable),
            ("alignable", self.alignable),
        ):
            if type(value) is not bool:
                raise TypeError(f"axis capability {name} must be a boolean, got {value!r}.")


_KIND_OPERATIONS: Mapping[AggregationKind, frozenset[AggregationOperation]] = MappingProxyType(
    {
        AggregationKind.SUM: frozenset({AggregationOperation.SUM}),
        AggregationKind.LAST: frozenset({AggregationOperation.LAST}),
        AggregationKind.MIN: frozenset({AggregationOperation.MIN}),
        AggregationKind.MAX: frozenset({AggregationOperation.MAX}),
        AggregationKind.EXACT_DISTINCT: frozenset(),
        AggregationKind.MERGE_SKETCH: frozenset({AggregationOperation.MERGE}),
        # A ratio value is never rolled up. RatioOfSumsPolicy aggregates each operand under its own
        # SUM policy and divides only after both aggregates exist.
        AggregationKind.RATIO_OF_SUMS: frozenset(),
        AggregationKind.NON_AGGREGATABLE: frozenset(),
    }
)


NETWORK_AXES = frozenset({SemanticAxis.ECOSYSTEM, SemanticAxis.PLATFORM})
"""The axes one `ecosystem_aggregation` declaration governs (ADR-106)."""

_NETWORK_BEHAVIORS = frozenset({AxisBehavior.ADDITIVE, AxisBehavior.EXACT_ONLY})


@dataclass(frozen=True, slots=True)
class AggregationPolicy:
    """Measure algebra plus complete behavior on every available canonical axis.

    `ecosystem_aggregation` is the metric's declared rule across networks (ADR-106): `additive`
    or `exact_only`, applied to both the ecosystem and the platform axis. The metric dictionary
    always declares it, and loading fails without it. When declared, every available network
    axis must carry exactly that behavior, so the declaration and the axis rules can't disagree.
    """

    kind: AggregationKind
    available_axes: frozenset[SemanticAxis]
    axes: Mapping[SemanticAxis, AxisPolicy]
    ecosystem_aggregation: AxisBehavior | None = None

    def __post_init__(self) -> None:
        if self.kind is AggregationKind.RATIO_OF_SUMS:
            raise SemanticCompatibilityError(
                "ratio_of_sums needs RatioOfSumsPolicy with two operand policies; a ratio value "
                "cannot be rolled up directly."
            )
        if not self.available_axes:
            raise SemanticCompatibilityError("aggregation policy must declare available axes.")
        if set(self.axes) != set(self.available_axes):
            missing = sorted(axis.value for axis in self.available_axes - set(self.axes))
            extra = sorted(axis.value for axis in set(self.axes) - self.available_axes)
            raise SemanticCompatibilityError(
                "aggregation axis rules are incomplete"
                + (f"; missing {', '.join(missing)}" if missing else "")
                + (f"; unknown {', '.join(extra)}" if extra else "")
                + "."
            )
        permitted = _KIND_OPERATIONS[self.kind]
        contradictory = sorted(
            axis.value
            for axis, policy in self.axes.items()
            if any(policy.aggregation.permits(operation) for operation in AggregationOperation)
            and not any(policy.aggregation.permits(operation) for operation in permitted)
            and not self._distinct_sums_disjoint(policy.aggregation)
        )
        if contradictory:
            raise SemanticCompatibilityError(
                f"{self.kind.value} declares incompatible axis algebra on "
                f"{', '.join(contradictory)}."
            )
        self._check_ecosystem_aggregation()
        self._check_disjoint_axes()
        object.__setattr__(self, "axes", MappingProxyType(dict(self.axes)))

    def _distinct_sums_disjoint(self, behavior: AxisBehavior) -> bool:
        """Allow an exact distinct count to sum across a disjoint axis, and nowhere else (D-3)."""
        return (
            self.kind is AggregationKind.EXACT_DISTINCT
            and behavior is AxisBehavior.DISJOINT_WITHIN_EXTRACTION
        )

    def _check_disjoint_axes(self) -> None:
        """Keep `disjoint_within_extraction` on the disjoint axes, inside one delivery (D-3, D-9).

        It may appear only on `DISJOINT_AXES`; a disjoint axis is either that or `exact_only`; and
        a policy that sums within an extraction is `exact_only` on every other axis, so the sum
        can never cross a delivery's period, marketplace, campaign or ad product.
        """
        disjoint = {
            axis
            for axis, policy in self.axes.items()
            if policy.aggregation is AxisBehavior.DISJOINT_WITHIN_EXTRACTION
        }
        misplaced = sorted(axis.value for axis in disjoint - DISJOINT_AXES)
        if misplaced:
            raise SemanticCompatibilityError(
                f"disjoint_within_extraction is only for disjoint shopper axes, not "
                f"{', '.join(misplaced)}."
            )
        loose = sorted(
            axis.value
            for axis in DISJOINT_AXES & self.available_axes
            if self.axes[axis].aggregation
            not in {AxisBehavior.DISJOINT_WITHIN_EXTRACTION, AxisBehavior.EXACT_ONLY}
        )
        if loose:
            raise SemanticCompatibilityError(
                f"{', '.join(loose)} must be disjoint_within_extraction or exact_only."
            )
        if not disjoint:
            return
        open_axes = sorted(
            axis.value
            for axis, policy in self.axes.items()
            if axis not in DISJOINT_AXES and policy.aggregation is not AxisBehavior.EXACT_ONLY
        )
        if open_axes:
            raise SemanticCompatibilityError(
                f"a sum within one extraction must be exact_only on every other axis; "
                f"{', '.join(open_axes)} is not."
            )

    def _check_ecosystem_aggregation(self) -> None:
        declared = self.ecosystem_aggregation
        if declared is None:
            return
        if declared not in _NETWORK_BEHAVIORS:
            raise SemanticCompatibilityError(
                f"ecosystem_aggregation is {declared.value}, expected additive or exact_only."
            )
        disagreeing = sorted(
            axis.value
            for axis in NETWORK_AXES & self.available_axes
            if self.axes[axis].aggregation is not declared
        )
        if disagreeing:
            raise SemanticCompatibilityError(
                f"ecosystem_aggregation is {declared.value}, but {', '.join(disagreeing)} "
                "declares another behavior."
            )

    def require_rollup(
        self,
        omitted_axes: frozenset[SemanticAxis],
        *,
        operation: AggregationOperation,
    ) -> None:
        """Refuse algebra or axes that do not permit the requested deterministic operation.

        An exact distinct count has no roll-up of its own; it may sum only when every omitted axis
        is a disjoint axis declared `disjoint_within_extraction` (D-3).
        """
        distinct_disjoint_sum = (
            operation is AggregationOperation.SUM
            and bool(omitted_axes)
            and omitted_axes <= self.available_axes
            and all(
                self._distinct_sums_disjoint(self.axes[axis].aggregation) for axis in omitted_axes
            )
        )
        if operation not in _KIND_OPERATIONS[self.kind] and not distinct_disjoint_sum:
            raise SemanticCompatibilityError(
                f"{self.kind.value} cannot be rolled up with {operation.value}."
            )
        for axis in sorted(omitted_axes, key=lambda item: item.value):
            if axis not in self.available_axes:
                raise SemanticCompatibilityError(
                    f"{self.kind.value}: axis {axis.value} is not available on this metric."
                )
            behavior = self.axes[axis].aggregation
            if not behavior.permits(operation):
                raise SemanticCompatibilityError(
                    f"{self.kind.value}: {axis.value} is {behavior.value}, so it cannot use "
                    f"{operation.value}."
                )

    def require_groupable(self, axes: frozenset[SemanticAxis]) -> None:
        """Refuse a requested breakdown on any axis the metric cannot expose."""
        self._require_capability(axes, capability="groupable")

    def require_filterable(self, axes: frozenset[SemanticAxis]) -> None:
        """Refuse a selector on any axis the metric cannot pin."""
        self._require_capability(axes, capability="filterable")

    def require_alignable(self, axes: frozenset[SemanticAxis]) -> None:
        """Refuse joining this measure on any axis whose identity cannot align."""
        self._require_capability(axes, capability="alignable")

    def _require_capability(self, axes: frozenset[SemanticAxis], *, capability: str) -> None:
        for axis in sorted(axes, key=lambda item: item.value):
            if axis not in self.available_axes:
                raise SemanticCompatibilityError(
                    f"{self.kind.value}: axis {axis.value} is not available on this metric."
                )
            if not bool(getattr(self.axes[axis], capability)):
                raise SemanticCompatibilityError(
                    f"{self.kind.value}: axis {axis.value} is not {capability}."
                )


@dataclass(frozen=True, slots=True)
class RatioOfAggregatesPolicy:
    """Two operands aggregated under their own algebra and divided only afterward."""

    numerator: AggregationPolicy
    denominator: AggregationPolicy

    def require_capabilities(
        self,
        groups: frozenset[SemanticAxis],
        filters: frozenset[SemanticAxis],
    ) -> None:
        """Require grouping, filtering and alignment independently on both operands."""
        for operand in (self.numerator, self.denominator):
            operand.require_groupable(groups)
            operand.require_alignable(groups)
            operand.require_filterable(filters & operand.available_axes)

    def require_operand_rollup(
        self,
        numerator_omitted: frozenset[SemanticAxis],
        denominator_omitted: frozenset[SemanticAxis],
    ) -> None:
        """Delegate rollup to each operand; exact operands accept only an empty omission."""
        for operand, omitted in (
            (self.numerator, numerator_omitted),
            (self.denominator, denominator_omitted),
        ):
            if omitted:
                operand.require_rollup(omitted, operation=AggregationOperation.SUM)


@dataclass(frozen=True, slots=True)
class RatioOfSumsPolicy:
    """Two additive operands aggregated independently and divided only afterward.

    There is deliberately no operation that aggregates the ratio value. Rollup delegates to both
    operand policies, which must each prove SUM on the omitted axes; the planner then divides the
    resulting aggregates.
    """

    numerator: AggregationPolicy
    denominator: AggregationPolicy

    def __post_init__(self) -> None:
        for label, operand in (("numerator", self.numerator), ("denominator", self.denominator)):
            if operand.kind is not AggregationKind.SUM:
                raise SemanticCompatibilityError(
                    f"ratio_of_sums {label} must use SUM algebra, got {operand.kind.value}."
                )

    def require_operand_rollup(self, omitted_axes: frozenset[SemanticAxis]) -> None:
        """Prove both operands can sum across the omitted axes before division."""
        self.numerator.require_rollup(omitted_axes, operation=AggregationOperation.SUM)
        self.denominator.require_rollup(omitted_axes, operation=AggregationOperation.SUM)

    def require_groupable(self, axes: frozenset[SemanticAxis]) -> None:
        """Require both operands to expose every requested result axis."""
        self.numerator.require_groupable(axes)
        self.denominator.require_groupable(axes)

    def require_filterable(self, axes: frozenset[SemanticAxis]) -> None:
        """Require both operands to accept every requested selector axis."""
        self.numerator.require_filterable(axes)
        self.denominator.require_filterable(axes)

    def require_alignable(self, axes: frozenset[SemanticAxis]) -> None:
        """Require both operands to align on every drill-across axis."""
        self.numerator.require_alignable(axes)
        self.denominator.require_alignable(axes)


class CompatibilityField(StrEnum):
    """Parts of a measurement signature a composition may compare explicitly."""

    BUSINESS_PROCESS = "business_process"
    METRIC_SEMANTICS = "metric_semantics"
    MEASUREMENT_BASIS = "measurement_basis"
    MEASUREMENT_METHOD = "measurement_method"
    POPULATION = "population"
    SOURCE_TIMEZONE = "source_timezone"
    BUSINESS_CALENDAR = "business_calendar"
    TIME_CLASS = "time_class"
    TIME_GRAIN = "time_grain"
    CURRENCY = "currency"
    FEE_INCLUSION = "fee_inclusion"
    ATTRIBUTION_WINDOW = "attribution_window"
    LOOKBACK_WINDOW = "lookback_window"
    PROFILE_VERSION = "profile_version"
    SOURCE_OPTION = "source_option"


def _require_signature_types(population: object, calendar_key: object) -> None:
    """Protect the runtime boundary when declarations arrive through untyped YAML adapters."""
    if not isinstance(population, CommerceScopeKey):
        raise TypeError("compatibility population must be a canonical commerce scope key.")
    if not isinstance(calendar_key, BusinessCalendarKey):
        raise TypeError("compatibility calendar must be a governed calendar key.")


@dataclass(frozen=True, slots=True)
class CompatibilitySignature:
    """Meaning that must be preserved before two measurements are combined."""

    business_process: str
    metric_semantics: str
    measurement_basis: MeasurementBasis
    measurement_method: str
    population: CommerceScopeKey
    source_timezone: str
    business_calendar: BusinessCalendarKey
    time_class: FactTimeClass
    time_grain: TimeGrain
    currency: str | None
    profile_version: int
    attribution_window_days: int | None = None
    lookback_months: int | None = None

    def __post_init__(self) -> None:
        required = {
            "business_process": self.business_process,
            "metric_semantics": self.metric_semantics,
            "measurement_method": self.measurement_method,
            "source_timezone": self.source_timezone,
        }
        blank = [name for name, value in required.items() if not value.strip()]
        if blank:
            raise SemanticCompatibilityError(
                f"compatibility signature is missing: {', '.join(sorted(blank))}."
            )
        _require_signature_types(self.population, self.business_calendar)
        if self.profile_version < 1:
            raise SemanticCompatibilityError("profile version starts at 1.")
        if self.attribution_window_days is not None and self.attribution_window_days < 1:
            raise SemanticCompatibilityError("attribution window must be positive when present.")
        if self.lookback_months is not None and self.lookback_months < 1:
            raise SemanticCompatibilityError("lookback window must be positive when present.")


@dataclass(frozen=True, slots=True)
class CompatibilityDifference:
    """One explicit semantic difference between two measurements."""

    field: CompatibilityField
    left: object
    right: object


class CompositionOperation(StrEnum):
    """Why two measurements are being related."""

    SUM = "sum"
    RATIO = "ratio"
    RECONCILIATION = "reconciliation"
    COMPARISON = "comparison"


_OPERATION_DIFFERENCES: Mapping[CompositionOperation, frozenset[CompatibilityField]] = (
    MappingProxyType(
        {
            CompositionOperation.SUM: frozenset(),
            CompositionOperation.RATIO: frozenset(
                {
                    CompatibilityField.BUSINESS_PROCESS,
                    CompatibilityField.METRIC_SEMANTICS,
                    CompatibilityField.MEASUREMENT_BASIS,
                    CompatibilityField.MEASUREMENT_METHOD,
                    CompatibilityField.SOURCE_TIMEZONE,
                    CompatibilityField.CURRENCY,
                    CompatibilityField.FEE_INCLUSION,
                    CompatibilityField.ATTRIBUTION_WINDOW,
                    CompatibilityField.LOOKBACK_WINDOW,
                    CompatibilityField.SOURCE_OPTION,
                }
            ),
            CompositionOperation.RECONCILIATION: frozenset(
                {
                    CompatibilityField.METRIC_SEMANTICS,
                    CompatibilityField.MEASUREMENT_BASIS,
                    CompatibilityField.MEASUREMENT_METHOD,
                }
            ),
            CompositionOperation.COMPARISON: frozenset({CompatibilityField.PROFILE_VERSION}),
        }
    )
)


@dataclass(frozen=True, slots=True)
class CompositionRule:
    """Differences an operation permits and must carry as structured qualifiers."""

    operation: CompositionOperation
    allowed_differences: frozenset[CompatibilityField] = frozenset()
    required_qualifiers: frozenset[CompatibilityField] = frozenset()

    def __post_init__(self) -> None:
        illegal = self.allowed_differences - _OPERATION_DIFFERENCES[self.operation]
        if illegal:
            raise SemanticCompatibilityError(
                f"{self.operation.value} can never permit: "
                f"{', '.join(sorted(field.value for field in illegal))}."
            )
        missing = self.allowed_differences - self.required_qualifiers
        if missing:
            raise SemanticCompatibilityError(
                "every permitted semantic difference needs a structured qualifier: "
                f"{', '.join(sorted(field.value for field in missing))}."
            )
        extraneous = self.required_qualifiers - self.allowed_differences
        if extraneous:
            raise SemanticCompatibilityError(
                "qualifiers cannot describe differences the operation does not permit: "
                f"{', '.join(sorted(field.value for field in extraneous))}."
            )


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    """Accepted differences and the qualifiers downstream publication must retain."""

    differences: tuple[CompatibilityDifference, ...]
    required_qualifiers: frozenset[CompatibilityField]


def compare_signatures(
    left: CompatibilitySignature, right: CompatibilitySignature
) -> tuple[CompatibilityDifference, ...]:
    """Return every semantic difference; never stop at the first correction."""
    pairs = (
        (CompatibilityField.BUSINESS_PROCESS, left.business_process, right.business_process),
        (CompatibilityField.METRIC_SEMANTICS, left.metric_semantics, right.metric_semantics),
        (CompatibilityField.MEASUREMENT_BASIS, left.measurement_basis, right.measurement_basis),
        (CompatibilityField.MEASUREMENT_METHOD, left.measurement_method, right.measurement_method),
        (CompatibilityField.POPULATION, left.population, right.population),
        (CompatibilityField.SOURCE_TIMEZONE, left.source_timezone, right.source_timezone),
        (
            CompatibilityField.BUSINESS_CALENDAR,
            left.business_calendar,
            right.business_calendar,
        ),
        (CompatibilityField.TIME_CLASS, left.time_class, right.time_class),
        (CompatibilityField.TIME_GRAIN, left.time_grain, right.time_grain),
        (CompatibilityField.CURRENCY, left.currency, right.currency),
        (
            CompatibilityField.ATTRIBUTION_WINDOW,
            left.attribution_window_days,
            right.attribution_window_days,
        ),
        (CompatibilityField.LOOKBACK_WINDOW, left.lookback_months, right.lookback_months),
        (CompatibilityField.PROFILE_VERSION, left.profile_version, right.profile_version),
    )
    return tuple(
        CompatibilityDifference(field=field, left=mine, right=theirs)
        for field, mine, theirs in pairs
        if mine != theirs
    )


def validate_composition(
    left: CompatibilitySignature,
    right: CompatibilitySignature,
    rule: CompositionRule,
) -> CompatibilityResult:
    """Accept only qualified differences legal for the operation."""
    differences = compare_signatures(left, right)
    forbidden = tuple(
        difference for difference in differences if difference.field not in rule.allowed_differences
    )
    if forbidden:
        rendered = ", ".join(difference.field.value for difference in forbidden)
        raise SemanticCompatibilityError(
            f"{rule.operation.value} would erase incompatible semantics: {rendered}."
        )
    return CompatibilityResult(
        differences=differences,
        required_qualifiers=rule.required_qualifiers,
    )
