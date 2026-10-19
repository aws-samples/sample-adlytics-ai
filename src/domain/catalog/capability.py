# SPDX-License-Identifier: MIT-0
"""What can be asked, at what grain, and why anything else is refused.

The capability half of the catalog tool. It answers from the metric dictionary alone: no
fact table, no Athena, no freshness registry. Coverage — what period is actually loaded and
how fresh it is — is the separate read path over publication state.

`catalog` matters more than it looks: without it the agent guesses at its own capabilities,
and that is where confident wrong answers come from (docs/answerable-questions.md §8).

**No metric is defined here.** Definitions live in `contracts/shared/metric-dictionary.yaml`
and are read from there, per the hexagonal steering. This module owns what a metric
*declaration means* — whether a grain is supported, why a request is refused — which is
logic rather than data.

**The structural refusals in docs/answerable-questions.md section 6 are deliberately not
encoded here.** Those eleven items are prose with reasons and alternatives, that document
owns them, and copying them into an enum would make a second home that drifts. What this
module refuses is what the dictionary itself states: a name it does not contain, a name
under `deferred`, a grain outside `valid_grains`, and a metric whose source options are not
met.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from domain.capabilities.paid_features import FEATURE_OBSERVATION_PERIODS
from domain.narrative.shaped_response import UnitKind
from domain.semantics.analytical_resolution import normalize_exact_text
from domain.semantics.compatibility import AggregationKind, AggregationPolicy, AxisBehavior
from domain.semantics.identity import CommerceScopeKey, PlatformKey
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, TimeGrain
from domain.shared_kernel.errors import AdlyticsError, DomainError, ErrorCode, ValidationError
from domain.shared_kernel.provenance import MeasurementBasis

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from domain.capabilities.paid_features import PaidFeatureState

__all__ = [
    "PERIOD_GRAIN_SELECTOR",
    "Catalog",
    "CatalogError",
    "CrossDeliveryException",
    "DeferredMetric",
    "DeliveryRatio",
    "DeploymentRefusalRequest",
    "FavourableDirection",
    "InventoryCategory",
    "Metric",
    "MetricCompatibilityMode",
    "MetricKind",
    "Refusal",
    "RefusalReason",
    "composite_operand_names",
    "facts_in_definition",
    "references_in_definition",
]

# Matches a canonical table and column inside a declared expression:
# `sum(fct_ad_exposure.spend)` yields `("fct_ad_exposure", "spend")`. The prefixes are the
# ones docs/canonical-model.md uses, so a table that is none of them is not a fact and is
# correctly not matched.
#
# This is the one statement of what a fact reference looks like. `scripts/check-lineage.py`
# reads it from here rather than carrying its own copy: it asks a different question — does
# some contract produce that column — but off the same grammar, and two copies would
# eventually disagree about what counts as a reference.
FACT_REFERENCE = re.compile(r"\b((?:fct|dim|snap)_[a-z0-9_]+)\.([a-z0-9_]+)")


class CatalogError(Exception):
    """A metric declaration cannot be interpreted."""


PRODUCT_OPERAND_COUNT = 2
"""Every declared product has exactly two operands, mirroring the ratio shape."""


class RatioScale(StrEnum):
    """How a ratio metric reads: a share-like rate or an x-times multiplier (design §4.3).

    A rate's change is in percentage points and a multiplier's in times, so materiality and
    the near-zero prior bound are sized per scale, never shared.
    """

    RATE = "rate"
    MULTIPLIER = "multiplier"


class FavourableDirection(StrEnum):
    """Which way a metric's change is good news (ADR-121 decision 2).

    `none` is a reviewed declaration, not a missing one: spend moving says nothing good or bad
    on its own, so its change always reads neutral.
    """

    UP = "up"
    DOWN = "down"
    NONE = "none"


IDENTITY_SPACE_SELECTOR = "identity_space"
"""The shopper-aggregate selector a rate pins to `account` (ADR-101; design §6.4)."""

COHORT_PATH_SELECTOR = "cohort_path"
"""The cohort selector naming a repeat source, `standard` or `arp` (ADR-104; design §6.6)."""

PROFILE_PATH_SELECTOR = "profile_path"
"""The buyer-profile selector naming the profiled base one delivery measures (design §6.5):
`asi_in_market` and `asi_lifestyle` (packs 10-11, paid) or `dsp_exposed_only` (pack 12)."""

PERIOD_GRAIN_SELECTOR = "time_grain"
"""The exact selector a periodic metric requires (ADR-100 point 1, design D-7)."""

_PERIOD_GRAINS = (TimeGrain.WEEK, TimeGrain.MONTH, TimeGrain.QUARTER, TimeGrain.CUSTOM)
"""Canonical order of the grains a periodic metric may declare (ADR-110): the calendar grains,
then the cohort vintage (ADR-104)."""


class CrossDeliveryException(StrEnum):
    """The one declared ratio across two shopper-aggregate contracts (design D-8 (b)).

    Every other figure that combines two deliveries for one period is refused, because each
    delivery has its own suppression and timing and so its own population.
    """

    ARP_COHORT_RATIO = "arp_cohort_ratio"
    """Pack 15 repeat buyers over pack 14 `cohort_buyers_account_ids`, same vintage and cell,
    withheld when the cohort-size check fails (design §6.6)."""


class DeliveryRatio(StrEnum):
    """A ratio of one row of a delivery to that delivery's own total row (design §6.5 C2).

    The segment row and the `profiled_total` row are two rows of one delivery, so no one query's
    row holds both operands. The measurement tool plans each operand and pairs them per segment
    through the domain rule the Audiences bake reads too
    (`domain.presentation.audiences.segment_figures`); a single plan never divides them.
    """

    SEGMENT_PROFILE = "segment_profile"
    """A segment's share of its delivery's profiled base, or the index of two such shares; every
    share and index of a delivery whose total row is missing reads "not available"."""


@dataclass(frozen=True, slots=True)
class WithinDelivery:
    """A ratio computed across the cells of one shopper-aggregate delivery (design §6.3, §6.4).

    `numerator` and `denominator` pin cells by grain (`exposure_group`, `identity_space`); every
    disjoint axis a side leaves unpinned, and not named in `per`, is summed over all its values
    inside the one delivery (D-3), refused when any is `not_returned`. `per` names the grains the
    figure is given for, one value at a time (the index is per exposure group).

    `overlap_share` is DSP-and-sponsored shoppers over every exposure group's, on account IDs.
    The chat planner reads one cell per figure, so it refuses these with a typed reason; the Reach
    and funnel bake computes them from one delivery's cells (`domain.presentation.reach`).
    """

    numerator: tuple[tuple[str, str], ...]
    denominator: tuple[tuple[str, str], ...]
    per: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, pins in (("numerator", self.numerator), ("denominator", self.denominator)):
            names = tuple(name for name, _value in pins)
            if len(set(names)) != len(names):
                raise CatalogError(f"within_delivery {label} pins each grain once.")
        if {name for name, _value in self.numerator} & set(self.per):
            # The denominator may pin one: the index's reference cell is the sponsored-only group.
            raise CatalogError("within_delivery numerator never pins a grain it is given per.")

    def pinned(self, side: str) -> dict[str, str]:
        """Return one side's pinned cells, `numerator` or `denominator`."""
        return dict(self.numerator if side == "numerator" else self.denominator)


class MetricKind(StrEnum):
    """How a metric is computed. The dictionary's own `type` field."""

    SIMPLE = "simple"
    """An aggregate over one fact column."""

    RATIO = "ratio"
    """A quotient of two operands, each a metric or a raw fact column."""

    PRODUCT = "product"
    """A product of two operands, each a declared metric.

    Exists for `returns_value_estimate_retail` (returned units x window average selling
    price). The live query planner deliberately refuses it — a product's honest value is
    derived at publish time from its already-measured operands, the same way the campaign
    baker reconstructs portfolio ROAS — so declaring one here governs its meaning without
    promising a live measurement path.
    """


class MetricCompatibilityMode(StrEnum):
    """How a declaration authorizes semantic differences between operands."""

    STRICT = "strict"
    QUALIFIED_RATIO = "qualified_ratio"


class RefusalReason(StrEnum):
    """Why a request cannot be answered.

    Five reasons, and they are not interchangeable. The first two are about the metric, the
    third about the grain, the fourth about the customer's own source configuration, the fifth
    about what this deployment has received — and a caller has to be able to tell "nobody can
    ask this" from "you cannot ask this here".
    """

    UNKNOWN_METRIC = "unknown_metric"
    """No such metric is declared. The request is outside the contract."""

    NOT_YET_COMPUTABLE = "not_yet_computable"
    """Declared under `deferred`: a real metric whose facts are not built yet.

    Distinct from unknown on purpose. Answering "no such metric" to a name the dictionary
    lists would be wrong, and it is the difference between a typo and a roadmap item.
    """

    GRAIN_NOT_SUPPORTED = "grain_not_supported"
    """The metric exists but not at the requested grain.

    The case this exists for is `roas_blended` at campaign grain: retail revenue carries no
    campaign, so the figure is computable, plausible and wrong.
    """

    REQUIRES_SOURCE_OPTION = "requires_source_option"
    """The metric needs a source option this data was not pulled under.

    `conversion_rate_retail` needs `distributor_view: MANUFACTURING`. Not a permanent
    refusal, which is why it is not folded into the others.
    """

    NOT_OBSERVED_IN_DEPLOYMENT = "not_observed_in_deployment"
    """The metric needs an AMC paid feature that has delivered nothing here lately (ADR-107).

    "Not licensed here", not "impossible": the same request is answerable in a deployment whose
    paid contract has published. Derived from delivery evidence, so it never claims the customer
    isn't subscribed — a producer that has failed looks the same.
    """


# One code per reason, so a rate can be alarmed on or ignored per reason rather than for
# "the catalog refused something". None of these alarm: DomainError and ValidationError are
# product signals, not health signals (ADR-010).
UNKNOWN_METRIC_CODE = ErrorCode(area="CATALOG", number=1)
NOT_COMPUTABLE_CODE = ErrorCode(area="CATALOG", number=2)
GRAIN_NOT_SUPPORTED_CODE = ErrorCode(area="CATALOG", number=3)
REQUIRES_OPTION_CODE = ErrorCode(area="CATALOG", number=4)
NOT_OBSERVED_IN_DEPLOYMENT_CODE = ErrorCode(area="CATALOG", number=5)

_CODES: dict[RefusalReason, ErrorCode] = {
    RefusalReason.UNKNOWN_METRIC: UNKNOWN_METRIC_CODE,
    RefusalReason.NOT_YET_COMPUTABLE: NOT_COMPUTABLE_CODE,
    RefusalReason.GRAIN_NOT_SUPPORTED: GRAIN_NOT_SUPPORTED_CODE,
    RefusalReason.REQUIRES_SOURCE_OPTION: REQUIRES_OPTION_CODE,
    RefusalReason.NOT_OBSERVED_IN_DEPLOYMENT: NOT_OBSERVED_IN_DEPLOYMENT_CODE,
}


@dataclass(frozen=True, slots=True)
class DeploymentRefusalRequest:
    """A catalog question plus what this deployment has received of each paid feature."""

    name: str
    grain: str | None = None
    filters: Mapping[str, str] | None = None
    paid_feature_states: Mapping[str, PaidFeatureState] = field(
        default_factory=dict[str, "PaidFeatureState"]
    )
    """Metric name to the state an adapter derived from Publication State (ADR-107).

    Only metrics that need an AMC paid feature appear. A metric absent here is not refused on
    this ground, because a standard metric has nothing to be observed. Only `feature_observed`
    is read, and it is deployment-level, so the state may be derived for any declared period.
    """


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refusal with its reason stated, never a bare failure.

    `offer_instead` exists because a refusal a user cannot act on reads as a broken product.
    docs/answerable-questions.md pairs every never-answerable item with an alternative for
    the same reason.
    """

    requested: str
    reason: RefusalReason
    explanation: str
    offer_instead: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.explanation:
            raise CatalogError(
                f"A refusal of {self.requested!r} must state why. An unexplained refusal is "
                "indistinguishable from a defect."
            )

    def to_error(self) -> AdlyticsError:
        """Turn a refusal into the error that carries it.

        **A refusal is raised, not returned as an empty response.** The distinction that
        settles it: zero rows means "we looked and there was nothing there", which ADR-010
        makes a legitimate response; a refusal means "we did not look, because" — and no
        query ran, so the response envelope would have to invent a measurement basis and a
        coverage window for figures that do not exist. `Metadata` requires both.

        Nothing is lost by raising. `description` is user-visible for these two classes, so
        the reason and the alternatives still reach the caller, and `context` carries the
        structured detail for the log.

        `ValidationError` for a name that is not declared — a request outside its contract.
        `DomainError` for the rest, where the request is understood and the answer is
        legitimately unavailable; its own docstring names "a metric that is not valid at the
        requested grain" as the case.
        """
        description = self.explanation
        if self.offer_instead:
            description = f"{description} Available instead: {', '.join(self.offer_instead)}."

        context = {"requested": self.requested, "reason": self.reason.value}
        code = _CODES[self.reason]
        if self.reason is RefusalReason.UNKNOWN_METRIC:
            return ValidationError(code=code, description=description, context=context)
        return DomainError(code=code, description=description, context=context)


@dataclass(frozen=True, slots=True)
class Metric:
    """One declared metric, as the dictionary states it."""

    name: str
    label: str
    kind: MetricKind
    unit_kind: UnitKind
    measurement_basis: tuple[MeasurementBasis, ...]
    valid_grains: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    time_class: FactTimeClass = FactTimeClass.DAILY_ADDITIVE
    time_grain: TimeGrain = TimeGrain.DAY
    compatibility_mode: MetricCompatibilityMode = MetricCompatibilityMode.STRICT
    population: CommerceScopeKey = field(default_factory=lambda: CommerceScopeKey("unscoped"))
    business_calendar: BusinessCalendarKey = field(
        default_factory=lambda: BusinessCalendarKey("unspecified")
    )
    profile_version: int = 1

    definition: str = ""
    """The declared expression, for a simple metric. Carried verbatim so a caller can show
    what a figure actually sums rather than paraphrasing it."""

    customer_definition: str = ""
    """Optional final customer-facing meaning governed by the metric dictionary."""

    customer_caveat: str = ""
    """Optional final customer-facing qualification governed by the metric dictionary."""

    numerator: str = ""
    denominator: str = ""
    operands: tuple[str, ...] = ()
    """A product's two declared metric operands, in declared order. Product only."""

    notes: str = ""

    source_system: str = ""
    """Which upstream system this metric's figures originate in. Required on a simple metric.

    Carried because `measurement_basis` does not separate two systems that measure the same
    thing. `retail_revenue_shipped` comes from Vendor Central and `retail_revenue_purchased`
    comes from AMC; both are `retail_actual`, both answer "what did I sell", and they will
    disagree — so `mixes_basis` stays false for a sum that is nonetheless a double count.
    The source is what makes that refusable.
    """

    source_datasets: tuple[str, ...] = ()
    """The upstream datasets the figures were extracted from. Required on a simple metric.

    Finer than the system, because one system yields several datasets that do not
    interchange: `dsp_impressions` and `sponsored_ads_traffic` are both `amc` and each carries
    a spend column on its own scale.

    Plural because a conformed fact is written by several of them. `fct_ad_exposure.spend`
    holds DSP and sponsored spend in one column, so `ad_spend` genuinely reads two datasets
    and naming one would be a false attribution rather than a simplification.
    """

    non_additive: bool = False
    """Whether summing this metric across rows produces a wrong number.

    True for a distinct count. `reach_users` is the case that matters: Monday's distinct
    shoppers plus Tuesday's is not two days' distinct shoppers, and AMC has no approximate
    distinct function, so no sketch can be exported and merged
    ([answerable-questions](../../../docs/answerable-questions.md) D-6). A stored rate such as
    `unit_price` is the other shape — averaging an average is wrong for a different reason.

    Declared rather than inferred from the unit kind, because `reach_users` and `impressions`
    are both counts and only one of them is summable.
    """

    multiplier: int = 1
    """A scalar the ratio is presented against, when the raw quotient is unreadable.

    Exists for `cpm`, which is cost per *thousand* impressions. Without it the choice is a
    metric named `cpm` that returns cost per single impression, or a tool that multiplies by
    a thousand it found nowhere in the declaration — and the second is the model-computes
    failure wearing an adapter's clothes.

    Ratio only. A simple metric is an aggregate of a declared column, so scaling one here
    would contradict the scale factor its contract already applied.
    """

    declared_mixes_basis: bool = False
    """What the dictionary says about this metric mixing measurement systems.

    Kept alongside the derived answer rather than replacing it: `roas_blended` declares it,
    and a declaration that disagreed with the basis list would be a contradiction worth
    seeing rather than silently resolving.
    """

    is_default_roas: bool = False
    includes_brand_halo: bool | None = None
    requires: tuple[tuple[str, str], ...] = ()
    """Source options this metric needs, as key/value pairs. Empty for most."""

    required_selectors: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Closed selector vocabularies where one explicit allowed value is mandatory."""

    aggregation_policy: AggregationPolicy | None = None
    """Complete axis behavior for a leaf metric; ratios delegate to their operands."""

    ecosystem_aggregation: AxisBehavior | None = None
    """How the metric aggregates across networks: the ecosystem and platform axes (ADR-106).

    The dictionary declares it on every entry under `metrics:`. On a simple metric it equals
    its policy's. On a composite it is exact_only unless every operand is additive
    (`Catalog` checks), and it never drives planning: each operand's own rule does.
    """

    source_platforms: frozenset[PlatformKey] = frozenset()
    """The advertising platforms a simple metric's figures can come from (ADR-106).

    Derived at load by `derive_source_platforms` from governed files, never declared. Empty
    on a composite, whose platforms are its operands'. Empty on a simple metric means
    unknown, and the planner then accounts neither network axis for it.
    """

    attribution_window_days: int | None = None
    lookback_months: int | None = None
    fee_inclusive: bool | None = None
    ratio_scale: RatioScale | None = None
    """Required exactly when the metric's units are a ratio (design §4.3)."""

    favourable_direction: FavourableDirection | None = None
    """Declared on every metric served with a comparison (ADR-121); `None` elsewhere.

    Optional here because the agent's tools compare any metric without a tone. The
    presentation's compared roster is checked when its change semantics load
    (`domain.presentation.change.change_semantics_from`).
    """

    period_grains: tuple[TimeGrain, ...] = ()
    """The complete calendar periods a periodic simple metric is measured over (ADR-100).

    Declared exactly on a `periodic` simple metric, in canonical order (week, month, quarter),
    and matched by its `time_grain` exact selector. Empty everywhere else: a ratio's periodic
    grains are its operands'.
    """

    cross_delivery: CrossDeliveryException | None = None
    """The declared D-8 (b) exception on a ratio across two shopper-aggregate contracts."""
    delivery_ratio: DeliveryRatio | None = None
    """A ratio of one row of a delivery to its own total row (C2), paired by the tool."""

    within_delivery: WithinDelivery | None = None
    """Where a ratio is computed across one delivery's cells rather than within one cell."""

    causal: bool | None = None
    """False on an association-only metric (ADR-102): groups aren't random, so a figure can't
    say an ad caused anything. Its chat answer carries the mandatory qualifier "association, not
    cause" and its visuals the caveat block. None where the dictionary doesn't say."""
    unsupported_grain_reason: str | None = None
    """Why the grains outside `valid_grains` are refused, where the reason is the metric's own.

    `roas_incl_halo_by_frequency` is served at the platform grain only, because per-channel
    return uses credited revenue (design §5.5, review DS6-2): the refusal names that, not just
    an unsupported breakdown.
    """

    def __post_init__(  # noqa: PLR0912 -- validates each independent metric invariant
        self,
    ) -> None:
        if not self.name:
            raise CatalogError("A metric requires a name.")
        self._check_aliases()
        selector_keys = tuple(key for key, _values in self.required_selectors)
        if len(set(selector_keys)) != len(selector_keys):
            raise CatalogError(f"{self.name}: required selector keys must be unique.")
        if set(selector_keys).intersection(key for key, _value in self.requires):
            raise CatalogError(f"{self.name}: selector cannot be both exact and one-of.")
        if any(
            not values or len(set(values)) != len(values)
            for _key, values in self.required_selectors
        ):
            raise CatalogError(
                f"{self.name}: required selector values must be nonempty and unique."
            )
        if not self.measurement_basis:
            raise CatalogError(f"{self.name}: measurement_basis is required on every metric.")
        if not self.valid_grains:
            raise CatalogError(
                f"{self.name}: at least one valid grain is required. A metric valid at no "
                "grain cannot be asked for at all, so declaring none is a mistake rather "
                "than a restriction."
            )
        if self.kind is MetricKind.RATIO and not (self.numerator and self.denominator):
            raise CatalogError(f"{self.name}: a ratio requires a numerator and a denominator.")
        if self.kind is MetricKind.PRODUCT:
            if len(self.operands) != PRODUCT_OPERAND_COUNT or not all(self.operands):
                raise CatalogError(f"{self.name}: a product requires exactly two named operands.")
            if self.definition or self.numerator or self.denominator:
                raise CatalogError(
                    f"{self.name}: a product declares only its operands; a definition or "
                    "quotient operands would be a second statement of what it computes."
                )
        elif self.operands:
            raise CatalogError(f"{self.name}: only a product metric may declare operands.")
        if self.kind is MetricKind.SIMPLE and not self.definition:
            raise CatalogError(f"{self.name}: a simple metric requires a definition.")
        if self.kind is MetricKind.SIMPLE and self.aggregation_policy is None:
            raise CatalogError(f"{self.name}: a simple metric requires an aggregation policy.")
        if self.kind is not MetricKind.SIMPLE and self.aggregation_policy is not None:
            raise CatalogError(f"{self.name}: a composite delegates aggregation to its operands.")
        self._check_network_declarations()
        self._check_period_declarations()
        if (
            self.kind is MetricKind.SIMPLE
            and self.compatibility_mode is not MetricCompatibilityMode.STRICT
        ):
            raise CatalogError(f"{self.name}: leaf metric compatibility must be strict.")
        if self.attribution_window_days is not None and self.attribution_window_days < 1:
            raise CatalogError(f"{self.name}: attribution window must be positive.")
        if self.lookback_months is not None and self.lookback_months < 1:
            raise CatalogError(f"{self.name}: lookback must be positive.")
        self._check_source()
        if self.multiplier < 1:
            raise CatalogError(
                f"{self.name}: multiplier is {self.multiplier}, which would scale a figure "
                "toward zero or invert its sign."
            )
        if self.multiplier != 1 and self.kind is not MetricKind.RATIO:
            raise CatalogError(
                f"{self.name}: only a ratio may carry a multiplier. A simple metric sums a "
                "declared column whose contract already applied a scale factor, so scaling "
                "it again here would mean two divisors and no way to tell which is live."
            )

    def _check_network_declarations(self) -> None:
        """Keep the cross-network rule in one meaning on the metric and its policy (ADR-106)."""
        if self.kind is not MetricKind.SIMPLE and self.source_platforms:
            raise CatalogError(
                f"{self.name}: a composite's source platforms are its operands', so it "
                "can't carry its own."
            )
        policy = self.aggregation_policy
        if policy is not None and policy.ecosystem_aggregation != self.ecosystem_aggregation:
            raise CatalogError(
                f"{self.name}: ecosystem_aggregation differs between the metric and its "
                "aggregation policy."
            )

    def _check_period_declarations(self) -> None:
        """Require a periodic simple metric to name its grains and select one (ADR-100)."""
        if self.cross_delivery is not None and self.kind is not MetricKind.RATIO:
            raise CatalogError(f"{self.name}: only a ratio may declare a cross-delivery exception.")
        if self.within_delivery is not None and self.kind is not MetricKind.RATIO:
            raise CatalogError(f"{self.name}: only a ratio is computed within one delivery.")
        if self.delivery_ratio is not None and self.kind is not MetricKind.RATIO:
            raise CatalogError(f"{self.name}: only a ratio pairs a row with its delivery's total.")
        periodic_leaf = self.kind is MetricKind.SIMPLE and self.time_class is FactTimeClass.PERIODIC
        if not periodic_leaf:
            if self.period_grains:
                raise CatalogError(
                    f"{self.name}: only a periodic simple metric declares period_grains."
                )
            return
        if not self.period_grains or self.period_grains != tuple(
            grain for grain in _PERIOD_GRAINS if grain in self.period_grains
        ):
            raise CatalogError(
                f"{self.name}: a periodic metric declares period_grains from week, month and "
                "quarter, once each, in that order."
            )
        selectors = dict(self.required_selectors)
        expected = tuple(sorted(grain.value for grain in self.period_grains))
        if selectors.get(PERIOD_GRAIN_SELECTOR) != expected:
            raise CatalogError(
                f"{self.name}: a periodic metric requires the {PERIOD_GRAIN_SELECTOR} selector "
                "with exactly its period_grains, because week and month rows coexist in one fact "
                "and nothing may sum across them (D-7)."
            )

    def _check_aliases(self) -> None:
        """Require immutable, meaningful, exact-normalized unique aliases per metric."""
        if not isinstance(cast("object", self.aliases), tuple):
            raise CatalogError(f"{self.name}: aliases must be an immutable tuple.")
        normalized_aliases: set[str] = set()
        for alias in self.aliases:
            normalized = normalize_exact_text(alias)
            if not normalized:
                raise CatalogError(f"{self.name}: aliases must not be blank after normalization.")
            if normalized in normalized_aliases:
                raise CatalogError(
                    f"{self.name}: aliases must be unique after exact normalization."
                )
            normalized_aliases.add(normalized)

    def _check_source(self) -> None:
        """Require a source on a simple metric and forbid one on a ratio.

        Asymmetric on purpose. A simple metric reads one fact column, so it has exactly one
        source and must say which. A ratio's sources are its operands' sources, so declaring
        them here would be a second home for a fact `Catalog.sources_across` already derives
        — and the copy is what goes stale when an operand is repointed.
        """
        declared = (self.source_system, self.source_datasets)
        if self.kind is MetricKind.SIMPLE:
            names = ("source_system", "source_datasets")
            missing = [key for key, value in zip(names, declared, strict=True) if not value]
            if missing:
                raise CatalogError(
                    f"{self.name}: {', '.join(missing)} is required on a simple metric. Two "
                    "systems can measure the same thing under one measurement basis, so a "
                    "figure with no stated source cannot be told apart from one that does "
                    "not reconcile with it."
                )
            return
        if any(declared):
            raise CatalogError(
                f"{self.name}: a composite must not declare a source. Its sources are its "
                "operands', derived by Catalog.sources_across, and a declaration here would "
                "be a second copy that no longer moves when an operand does."
            )

    @property
    def mixes_basis(self) -> bool:
        """Whether this metric spans more than one measurement system."""
        return len(set(self.measurement_basis)) > 1

    @property
    def absence_is_typed(self) -> bool:
        """Whether a missing value is typed absent (`null`), never 0 (ADR-087, widened by ADR-099).

        A ratio with no row has no value. So has an exact distinct count with no row: too few
        shoppers, or none, and the two can't be told apart. An additive metric's absence from a
        fully covered answer is a measured 0. Decided from the dictionary, never from a name.
        """
        return self.kind is MetricKind.RATIO or (
            self.aggregation_policy is not None
            and self.aggregation_policy.kind is AggregationKind.EXACT_DISTINCT
        )

    def supports(self, grain: str) -> bool:
        """Whether this metric may be asked for at one grain."""
        return grain in self.valid_grains


def composite_operand_names(metric: Metric) -> tuple[str, ...]:
    """Name a composite metric's operands; a simple metric has none.

    The one statement of "which names does this metric resolve through", so a ratio's
    quotient operands and a product's factor operands are walked by the same code in
    `facts_read`, `sources_for`, `is_non_additive`, and the cycle check.
    """
    if metric.kind is MetricKind.RATIO:
        return (metric.numerator, metric.denominator)
    if metric.kind is MetricKind.PRODUCT:
        return metric.operands
    return ()


def references_in_definition(expression: str) -> tuple[tuple[str, str], ...]:
    """List every (table, column) a declared expression reads, in order of appearance."""
    return tuple(FACT_REFERENCE.findall(expression))


def facts_in_definition(expression: str) -> tuple[str, ...]:
    """Name the canonical tables a declared expression reads, deduplicated, in order.

    Derived from `references_in_definition` rather than using a second pattern, so "which
    tables" and "which columns" cannot come to disagree about what a reference is.
    """
    seen: list[str] = []
    for table, _column in references_in_definition(expression):
        if table not in seen:
            seen.append(table)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class InventoryCategory:
    """One declared ordered category for the complete computable metric inventory."""

    category_id: str
    label: str
    display_order: int
    metric_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.category_id):
            raise CatalogError("inventory category id must be a stable lowercase identifier.")
        if not self.label.strip():
            raise CatalogError(f"{self.category_id}: inventory category label is required.")
        if self.display_order < 1:
            raise CatalogError(
                f"{self.category_id}: inventory category display_order must be positive."
            )
        if not self.metric_ids:
            raise CatalogError(f"{self.category_id}: inventory category requires metrics.")
        if len(set(self.metric_ids)) != len(self.metric_ids):
            raise CatalogError(f"{self.category_id}: inventory category metric ids must be unique.")
        if any(not metric_id for metric_id in self.metric_ids):
            raise CatalogError(
                f"{self.category_id}: inventory category metric ids must not be blank."
            )


@dataclass(frozen=True, slots=True)
class DeferredMetric:
    """One separately declared metric whose facts are not yet computable."""

    name: str
    label: str
    kind: MetricKind
    unit_kind: UnitKind
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise CatalogError("a deferred metric requires a name.")
        if not self.label.strip():
            raise CatalogError(f"{self.name}: deferred metric label is required.")
        if not isinstance(cast("object", self.aliases), tuple):
            raise CatalogError(f"{self.name}: deferred metric aliases must be an immutable tuple.")
        if any(not alias.strip() for alias in self.aliases):
            raise CatalogError(f"{self.name}: deferred metric aliases must not be blank.")


@dataclass(frozen=True, slots=True)
class Catalog:
    """Every declared metric, its governed inventory categories, and what is deferred.

    Fact resolution lives here rather than on `Metric` because a ratio's operands name other
    metrics, so answering "which facts does `roas_blended` read" needs the whole dictionary
    and not one entry.
    """

    metrics: tuple[Metric, ...]
    deferred: tuple[DeferredMetric, ...]
    inventory_categories: tuple[InventoryCategory, ...]

    def __post_init__(  # noqa: PLR0912 -- validates independent catalog registry invariants
        self,
    ) -> None:
        names = [metric.name for metric in self.metrics]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise CatalogError(f"declared twice: {', '.join(duplicates)}.")
        defaults = sorted(metric.name for metric in self.metrics if metric.is_default_roas)
        if len(defaults) != 1:
            declared = ", ".join(defaults) if defaults else "none"
            raise CatalogError(
                f"exactly one metric must declare is_default_roas; found {declared}."
            )
        deferred_names = [metric.name for metric in self.deferred]
        deferred_duplicates = sorted(
            {name for name in deferred_names if deferred_names.count(name) > 1}
        )
        if deferred_duplicates:
            raise CatalogError(f"deferred declared twice: {', '.join(deferred_duplicates)}.")
        overlap = sorted(set(deferred_names).intersection(names))
        if overlap:
            raise CatalogError(
                f"{', '.join(overlap)} is both computable and deferred. One of the two is "
                "wrong, and guessing which would make a metric silently unavailable."
            )
        computable_surfaces = {
            normalize_exact_text(surface)
            for metric in self.metrics
            for surface in (metric.name, metric.label, *metric.aliases)
        }
        shadowed = sorted(
            deferred.name
            for deferred in self.deferred
            if {
                normalize_exact_text(deferred.name),
                normalize_exact_text(deferred.label),
                *(normalize_exact_text(alias) for alias in deferred.aliases),
            }
            & computable_surfaces
        )
        if shadowed:
            raise CatalogError(
                f"{', '.join(shadowed)}: a deferred name, label, or alias normalizes onto a "
                "computable metric surface. Deferred resolution runs before the computable "
                "lookup, so the collision would silently turn a computable metric into a "
                "not-yet-computable one."
            )
        if not self.inventory_categories:
            raise CatalogError("inventory_categories must not be empty.")
        category_ids = [category.category_id for category in self.inventory_categories]
        if len(set(category_ids)) != len(category_ids):
            raise CatalogError("inventory category ids must be unique.")
        display_orders = [category.display_order for category in self.inventory_categories]
        if len(set(display_orders)) != len(display_orders):
            raise CatalogError("inventory category display orders must be unique.")
        memberships = [
            metric_id for category in self.inventory_categories for metric_id in category.metric_ids
        ]
        unknown = sorted(set(memberships).difference(names))
        if unknown:
            raise CatalogError(
                f"inventory categories reference unknown metrics: {', '.join(unknown)}."
            )
        membership_duplicates = sorted(
            {metric_id for metric_id in memberships if memberships.count(metric_id) > 1}
        )
        if membership_duplicates:
            raise CatalogError(
                "inventory categories must own each computable metric once; repeated: "
                f"{', '.join(membership_duplicates)}."
            )
        unreferenced = sorted(set(names).difference(memberships))
        if unreferenced:
            raise CatalogError(
                "inventory categories must own every computable metric; unreferenced: "
                f"{', '.join(unreferenced)}."
            )
        by_name = {metric.name: metric for metric in self.metrics}
        for metric in self.metrics:
            if metric.kind is not MetricKind.SIMPLE:
                missing = [
                    operand for operand in composite_operand_names(metric) if operand not in by_name
                ]
                if missing:
                    raise CatalogError(
                        f"{metric.name}: unknown metric operands {', '.join(missing)}."
                    )
        _reject_metric_cycles(by_name)
        _check_composite_ecosystem_aggregation(by_name)

    def ordered_inventory_categories(self) -> tuple[InventoryCategory, ...]:
        """Return categories in their unique declared public display order."""
        return tuple(sorted(self.inventory_categories, key=lambda category: category.display_order))

    def is_deferred(self, name: str) -> bool:
        """Whether a reference names a metric declared separately as not yet computable."""
        return bool(self.deferred_candidates(name))

    def deferred_candidates(self, reference: str) -> tuple[DeferredMetric, ...]:
        """Find exact-normalized deferred canonical names and customer labels.

        The same matching contract as `metric_candidates`, so the customer-facing label or
        governed alias of a deferred metric reaches the not-yet-computable refusal instead of
        reading as a metric nobody declared.
        """
        expected = normalize_exact_text(reference)
        if not expected:
            return ()
        return tuple(
            sorted(
                (
                    metric
                    for metric in self.deferred
                    if expected
                    in {
                        normalize_exact_text(metric.name),
                        normalize_exact_text(metric.label),
                        *(normalize_exact_text(alias) for alias in metric.aliases),
                    }
                ),
                key=lambda metric: metric.name,
            )
        )

    def metric(self, name: str) -> Metric | None:
        """Find one metric, or None when it is not declared as computable."""
        return next((metric for metric in self.metrics if metric.name == name), None)

    def default_roas(self) -> Metric:
        """Return the one governed default guaranteed by catalog construction."""
        return next(metric for metric in self.metrics if metric.is_default_roas)

    def metric_candidates(self, reference: str) -> tuple[Metric, ...]:
        """Find exact-normalized canonical names, labels, and governed aliases.

        Matching deliberately performs no stemming, token reordering, substring, or fuzzy
        interpretation. A metric is returned once even when several of its surfaces normalize to
        the same reference, and canonical-name ordering makes ambiguity stable across dictionary
        declaration order.
        """
        expected = normalize_exact_text(reference)
        if not expected:
            return ()
        return tuple(
            sorted(
                (
                    metric
                    for metric in self.metrics
                    if expected
                    in {
                        normalize_exact_text(metric.name),
                        normalize_exact_text(metric.label),
                        *(normalize_exact_text(alias) for alias in metric.aliases),
                    }
                ),
                key=lambda metric: metric.name,
            )
        )

    def facts_read(self, metric: Metric) -> tuple[str, ...]:
        """Name the canonical facts a metric ultimately reads.

        A ratio resolves through its operands, each of which is either another metric or a
        raw fact column. One level is enough today and a deeper chain would still terminate,
        because an operand naming itself would have been caught as a cycle at load.
        """
        if metric.kind is MetricKind.SIMPLE:
            return facts_in_definition(metric.definition)

        found: list[str] = []
        for operand in composite_operand_names(metric):
            referenced = self.metric(operand)
            resolved = (
                self.facts_read(referenced)
                if referenced is not None
                # Not a metric name, so it is a raw fact column such as
                # `fct_ad_exposure.spend`.
                else facts_in_definition(operand)
            )
            for fact in resolved:
                if fact not in found:
                    found.append(fact)
        return tuple(found)

    def sources_for(self, metric: Metric) -> tuple[tuple[str, str], ...]:
        """Name the upstream sources a metric ultimately reads, as (system, dataset) pairs.

        Resolves a ratio through its operands, the same shape as `facts_read` and for the
        same reason: the operands are where the declaration lives.

        **A raw fact-column operand contributes nothing**, because a column names a fact and
        a fact does not name a dataset — `fct_ad_exposure` is written by four of them, so
        picking one would be a guess. Every ratio operand is a declared metric name today,
        which is what keeps this total; an operand written as a column is the shape that would
        reintroduce the gap.
        """
        if metric.kind is MetricKind.SIMPLE:
            return tuple((metric.source_system, dataset) for dataset in metric.source_datasets)

        found: list[tuple[str, str]] = []
        for operand in composite_operand_names(metric):
            referenced = self.metric(operand)
            if referenced is None:
                continue
            for pair in self.sources_for(referenced):
                if pair not in found:
                    found.append(pair)
        return tuple(found)

    def sources_across(self, metrics: tuple[Metric, ...]) -> tuple[tuple[str, str], ...]:
        """Every upstream source present across a set of metrics, deduplicated.

        The companion to `bases_across`, and it catches what that one cannot. Two sources can
        measure the same thing under one measurement basis — Vendor Central shipped revenue
        and AMC retail purchases are both `retail_actual` — so `mixes_basis` stays false for a
        combination that still does not reconcile. More than one pair here under a single
        basis is that case.
        """
        found: list[tuple[str, str]] = []
        for metric in metrics:
            for pair in self.sources_for(metric):
                if pair not in found:
                    found.append(pair)
        return tuple(found)

    @staticmethod
    def _unmet_options(
        metric: Metric, filters: Mapping[str, str] | None
    ) -> tuple[tuple[str, str], ...]:
        """List the source options this metric needs that the question has not pinned.

        **Checked against the filters rather than refused outright**, which is what the
        refusal has always claimed and never did. `units_ordered` is null under `SOURCING`, so
        it needs `distributor_view=MANUFACTURING` — a condition the reporting-lens default
        already satisfies on every question that reaches a fact carrying that column
        ([ADR-027](../decisions/adr-027-a-tool-may-default-a-lens-never-a-population.md)).
        Refusing anyway made every metric with a `requires` block permanently unanswerable
        whatever the data said, which is indistinguishable from not declaring it.

        A contradicting value is as unmet as an absent one, and deliberately not a separate
        reason: asking for `units_ordered` under `SOURCING` is asking for a column that is
        null by construction, and the answer is the same either way.
        """
        pinned = filters or {}
        exact = tuple((key, value) for key, value in metric.requires if pinned.get(key) != value)
        one_of = tuple(
            (key, f"one of ({', '.join(values)})")
            for key, values in metric.required_selectors
            if pinned.get(key) not in values
        )
        return (*exact, *one_of)

    def is_non_additive(self, metric: Metric) -> bool:
        """Whether summing this metric across rows would produce a wrong number.

        Resolved through operands rather than read off the entry, because a ratio inherits the
        property: `avg_frequency` divides impressions by `reach_users`, and a distinct count
        in the denominator makes the quotient meaningless at any grain coarser than the one
        the count was extracted at. Declaring it again on the ratio would be a second home for
        something the operand already states.
        """
        if metric.non_additive:
            return True
        if metric.kind is MetricKind.SIMPLE:
            return False
        return any(
            referenced is not None and self.is_non_additive(referenced)
            for referenced in (self.metric(operand) for operand in composite_operand_names(metric))
        )

    def bases_across(self, metrics: tuple[Metric, ...]) -> tuple[MeasurementBasis, ...]:
        """Every measurement basis present across a set of metrics, deduplicated.

        Order follows first appearance so a response is deterministic; `Metadata.mixes_basis`
        reads the distinct count, so a stable order changes nothing there but makes a
        response diffable in a test.
        """
        found: list[MeasurementBasis] = []
        for metric in metrics:
            for basis in metric.measurement_basis:
                if basis not in found:
                    found.append(basis)
        return tuple(found)

    def refusal_for(
        self,
        name: str,
        *,
        grain: str | None = None,
        filters: Mapping[str, str] | None = None,
    ) -> Refusal | None:
        """Decide whether a request can be honoured, and say why when it cannot.

        Ordered from the most specific reason to the least. Checking the grain before
        checking whether the metric exists would report a grain problem for a typo.

        `filters` is what the caller pinned, including the reporting-lens defaults a tool
        merged in. It is read only by the `requires` check, and passing None means the options
        are unknown — which refuses, because a metric that needs `MANUFACTURING` cannot be
        answered by a caller who has not said which view they are asking about.

        Whether this deployment has received a paid feature is a separate question, answered by
        `refusal_in_deployment`, which runs these checks first.
        """
        if self.is_deferred(name):
            return Refusal(
                requested=name,
                reason=RefusalReason.NOT_YET_COMPUTABLE,
                explanation=(
                    f"{name} is a declared metric, but the facts behind it are not built "
                    "yet, so no figure exists to return."
                ),
                offer_instead=(self.default_roas().name,),
            )

        metric = self.metric(name)
        if metric is None:
            return Refusal(
                requested=name,
                reason=RefusalReason.UNKNOWN_METRIC,
                explanation=f"{name} is not a metric this platform declares.",
                # Naming what does exist turns a dead end into a next step.
                offer_instead=tuple(candidate.name for candidate in self.metrics),
            )
        early = self.within_delivery_refusal(metric) or self._option_refusal(metric, filters)
        if early is not None:
            return early

        if grain is not None and not metric.supports(grain):
            reason = (
                ""
                if metric.unsupported_grain_reason is None
                else f" It is refused there because {metric.unsupported_grain_reason}."
            )
            return Refusal(
                requested=name,
                reason=RefusalReason.GRAIN_NOT_SUPPORTED,
                explanation=(
                    f"The requested {grain} breakdown is unsupported for {name}.{reason} A "
                    "figure at an unsupported breakdown is computable and wrong, which is worse "
                    "than absent."
                ),
                offer_instead=metric.valid_grains,
            )
        return None

    def _option_refusal(self, metric: Metric, filters: Mapping[str, str] | None) -> Refusal | None:
        """Refuse a metric whose mandatory selectors or source options the question didn't pin."""
        unmet = self._unmet_options(metric, filters)
        if metric.cross_delivery is CrossDeliveryException.ARP_COHORT_RATIO:
            # Its operands read one path each, arp and standard, and the measurement tool pins
            # both (owner decision 2026-10-12), so the question names no cohort path.
            unmet = tuple(item for item in unmet if item[0] != COHORT_PATH_SELECTOR)
        if any(key == IDENTITY_SPACE_SELECTOR for key, _value in unmet):
            return Refusal(
                requested=metric.name,
                reason=RefusalReason.REQUIRES_SOURCE_OPTION,
                explanation=(
                    f"{metric.name} reads Amazon account IDs only, because a device or browser ID "
                    "can't be matched to an account purchase (design §6.4). Pin "
                    "identity_space=account."
                ),
            )
        if any(key == PROFILE_PATH_SELECTOR for key, _value in unmet):
            paths = dict(unmet)[PROFILE_PATH_SELECTOR]
            return Refusal(
                requested=metric.name,
                reason=RefusalReason.REQUIRES_SOURCE_OPTION,
                explanation=(
                    f"{metric.name} reads one profile path, {paths}: asi_in_market and "
                    "asi_lifestyle profile the shoppers your ads reached with Amazon audience "
                    "segments (paid), dsp_exposed_only the DSP-exposed shoppers with DSP "
                    "behaviour segments. Each has its own base, so they're never compared or "
                    "added as one series. Pin profile_path."
                ),
            )
        if any(key == COHORT_PATH_SELECTOR for key, _value in unmet):
            paths = dict(unmet)[COHORT_PATH_SELECTOR]
            return Refusal(
                requested=metric.name,
                reason=RefusalReason.REQUIRES_SOURCE_OPTION,
                explanation=(
                    f"{metric.name} reads one cohort path, {paths}: standard counts ad-touched "
                    "repeats (pack 14), arp all Amazon purchases of Amazon account IDs (pack 15), "
                    "and the two are never one series. Pin cohort_path."
                ),
            )
        if not unmet:
            return None
        stated = ", ".join(f"{key}={value}" for key, value in unmet)
        return Refusal(
            requested=metric.name,
            reason=RefusalReason.REQUIRES_SOURCE_OPTION,
            explanation=(
                f"{metric.name} is only computable where the source data was pulled with "
                f"{stated}, and this question does not pin it. Whether that holds is a "
                "property of this customer's load, not of the platform, so the option has "
                "to be named rather than assumed."
            ),
        )

    @staticmethod
    def within_delivery_refusal(metric: Metric) -> Refusal | None:
        """Refuse a ratio computed across one delivery's cells, which the planner can't compute.

        The planner reads one cell per figure, so planning `overlap_share` as its operands would
        divide a cell by itself. Refused with a typed reason, offering the per-cell operand the
        planner does read; the Reach and funnel bake computes the figure (design §6.3).
        """
        if metric.within_delivery is None:
            return None
        return Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} is computed across every exposure group of one complete period's "
                "delivery, which this question path doesn't compute yet; Reach and funnel shows "
                f"it. Ask for {metric.numerator} by exposure group and identity space instead."
            ),
            offer_instead=(metric.numerator,),
        )

    def refusal_in_deployment(self, request: DeploymentRefusalRequest) -> Refusal | None:
        """Decide `refusal_for`, then whether this deployment has received the paid feature.

        The platform checks run first, so a request that is impossible for anyone says so
        rather than reading as "not licensed here" (ADR-107 point 4).
        """
        refusal = self.refusal_for(request.name, grain=request.grain, filters=request.filters)
        if refusal is not None:
            return refusal
        metric = self.metric(request.name)
        paid_state = None if metric is None else request.paid_feature_states.get(metric.name)
        if paid_state is None or paid_state.feature_observed:
            return None
        return Refusal(
            requested=request.name,
            reason=RefusalReason.NOT_OBSERVED_IN_DEPLOYMENT,
            explanation=(
                f"{request.name} needs an Amazon Marketing Cloud paid feature, and no delivery "
                f"of it has arrived in this deployment in the last {FEATURE_OBSERVATION_PERIODS} "
                "periods. That doesn't mean the customer isn't subscribed: a producer that "
                "hasn't delivered looks the same."
            ),
        )


def _reject_metric_cycles(metrics: dict[str, Metric]) -> None:
    """Reject operand cycles once, at catalog construction rather than recursive planning."""
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(name: str) -> None:
        if name in complete:
            return
        if name in visiting:
            raise CatalogError(f"metric operand cycle includes {name}.")
        visiting.add(name)
        metric = metrics[name]
        for operand in composite_operand_names(metric):
            visit(operand)
        visiting.remove(name)
        complete.add(name)

    for name in metrics:
        visit(name)


def _leaf_ecosystem_aggregations(
    metrics: dict[str, Metric], metric: Metric
) -> tuple[AxisBehavior | None, ...]:
    if metric.kind is MetricKind.SIMPLE:
        return (metric.ecosystem_aggregation,)
    return tuple(
        behavior
        for operand in composite_operand_names(metric)
        for behavior in _leaf_ecosystem_aggregations(metrics, metrics[operand])
    )


def _check_composite_ecosystem_aggregation(metrics: dict[str, Metric]) -> None:
    """Refuse a composite whose declared network rule isn't the one its operands imply.

    There is no ratio-level policy (ADR-106): the planner applies each operand's own rule. So a
    composite's declaration only records the outcome, `additive` exactly when every operand is
    additive, and a declaration that claims more than its operands allow is refused at load.
    Runs after the cycle check, so the walk terminates.
    """
    for metric in metrics.values():
        declared = metric.ecosystem_aggregation
        if metric.kind is MetricKind.SIMPLE or declared is None:
            continue
        leaves = _leaf_ecosystem_aggregations(metrics, metric)
        implied = (
            AxisBehavior.ADDITIVE
            if all(leaf is AxisBehavior.ADDITIVE for leaf in leaves)
            else AxisBehavior.EXACT_ONLY
        )
        if declared is not implied:
            raise CatalogError(
                f"{metric.name}: ecosystem_aggregation is {declared.value}, but its operands "
                f"imply {implied.value}."
            )
