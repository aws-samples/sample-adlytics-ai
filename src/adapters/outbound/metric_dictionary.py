# SPDX-License-Identifier: MIT-0
"""Read `contracts/shared/metric-dictionary.yaml` into the domain's catalog types.

The metric dictionary is the single declaration of what a metric means — the hexagonal
steering forbids putting a metric definition in code — and until now nothing under `src/`
read it. `pyproject.toml` carries PyYAML as a runtime rather than a dev dependency for
exactly this reason.

The adapter half: this module owns YAML, paths and file layout, and the rules that judge what
it produces live in `domain/catalog`. Same split as `contract_validation/loader.py`, and for
the same reason — it lets every rule be unit tested without a fixture file.

It also loads the data contracts, source packs and media taxonomy, because each simple metric
carries the platforms its figures can come from (ADR-106). The walk itself is domain code,
`derive_source_platforms`; this module only reads the files and calls it.

`yaml.safe_load`, never `yaml.load`. The full loader constructs arbitrary Python objects and
this file is read by a Lambda; ruff's S506 enforces it.

Every value is narrowed from `object` before use. This repository-owned executable contract fails
closed on unknown or duplicate fields and wrong scalar types; additive tolerance applies to upstream
source documents, never to metric semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import yaml

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.attribution_models import (
    REGISTRY_PATH as ATTRIBUTION_MODELS_PATH,
)
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.source_pack import load_source_packs
from domain.catalog.capability import (
    PERIOD_GRAIN_SELECTOR,
    Catalog,
    CatalogError,
    CrossDeliveryException,
    DeferredMetric,
    DeliveryRatio,
    FavourableDirection,
    InventoryCategory,
    Metric,
    MetricCompatibilityMode,
    MetricKind,
    RatioScale,
    WithinDelivery,
)
from domain.measurement.lenses import REPORT_LENSES
from domain.narrative.shaped_response import UnitKind
from domain.semantics.compatibility import (
    NETWORK_AXES,
    AggregationKind,
    AggregationPolicy,
    AxisBehavior,
    AxisPolicy,
    SemanticAxis,
    SemanticCompatibilityError,
)
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.source_platforms import SourcePlatformsRequest, derive_source_platforms
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, TimeGrain
from domain.shared_kernel.provenance import MeasurementBasis

DICTIONARY_PATH = Path("contracts/shared/metric-dictionary.yaml")

type Mapping = dict[str, object]


def _as_mapping(value: object, *, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise CatalogError(f"{where}: expected a mapping, got {type(value).__name__}.")
    return cast("Mapping", value)


def _as_str(value: object, *, key: str, where: str) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"{where}: {key} must be a string, got {type(value).__name__}.")
    return value


def _as_int(value: object, *, key: str, where: str) -> int:
    """Narrow to an int, refusing a bool.

    `isinstance(True, int)` is true in Python, so without the bool check `multiplier: yes`
    would load as 1 and silently mean nothing.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError(f"{where}: {key} must be an integer, got {type(value).__name__}.")
    return value


def _as_bool(value: object, *, key: str, where: str) -> bool:
    if type(value) is not bool:
        raise CatalogError(f"{where}: {key} must be a boolean, got {value!r}.")
    return value


def _optional_int(value: object, *, key: str, where: str) -> int | None:
    return None if value is None else _as_int(value, key=key, where=where)


def _as_str_tuple(value: object, *, key: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogError(f"{where}: {key} must be a list, got {type(value).__name__}.")
    return tuple(_as_str(item, key=f"{key}[]", where=where) for item in cast("list[object]", value))


def _unit_kinds(raw: Mapping, *, where: str) -> dict[str, UnitKind]:
    """Resolve the dictionary's named unit specs to the envelope's unit vocabulary.

    A metric's `units:` value names an entry in the top-level `units:` block rather than
    stating a kind directly, so resolving a metric's denomination is a second lookup. The
    indirection is the dictionary's, not ours: `currency` carries
    `currency_from: source_column`, recording that the currency code comes from the data and
    never from this file (ADR-021).
    """
    resolved: dict[str, UnitKind] = {}
    for name, spec_raw in raw.items():
        spec = _as_mapping(spec_raw, where=f"{where}: units.{name}")
        unknown = sorted(set(spec) - {"kind", "currency_from"})
        if unknown:
            raise CatalogError(f"{where}: units.{name} has unknown fields {', '.join(unknown)}.")
        kind = _as_str(spec.get("kind", ""), key=f"units.{name}.kind", where=where)
        try:
            resolved[name] = UnitKind(kind)
        except ValueError as error:
            raise CatalogError(
                f"{where}: units.{name}.kind is {kind!r}, which is not one of "
                f"{', '.join(member.value for member in UnitKind)}."
            ) from error
        currency_from = spec.get("currency_from")
        if resolved[name] is UnitKind.CURRENCY:
            if currency_from != "source_column":
                raise CatalogError(f"{where}: units.{name}.currency_from must be source_column.")
        elif currency_from is not None:
            raise CatalogError(f"{where}: units.{name} cannot declare currency_from for {kind}.")
    return resolved


def _bases(value: object, *, name: str, where: str) -> tuple[MeasurementBasis, ...]:
    """Read the declared measurement bases, rejecting one the domain does not know.

    Loud rather than skipped: a basis we cannot represent would silently drop out of
    `Metadata.measurement_basis`, and `mixes_basis` is derived from that — so a dropped basis
    turns a response that needs a caveat into one that appears not to.
    """
    declared = _as_str_tuple(value, key="measurement_basis", where=f"{where}: {name}")
    found: list[MeasurementBasis] = []
    for basis in declared:
        try:
            found.append(MeasurementBasis(basis))
        except ValueError as error:
            raise CatalogError(
                f"{where}: {name}.measurement_basis contains {basis!r}, which is not one of "
                f"{', '.join(member.value for member in MeasurementBasis)}."
            ) from error
    return tuple(found)


_GRAIN_AXES = {
    "date": SemanticAxis.TIME,
    "ecosystem": SemanticAxis.ECOSYSTEM,
    "platform": SemanticAxis.PLATFORM,
    "channel_group": SemanticAxis.CHANNEL_GROUP,
    "channel": SemanticAxis.CHANNEL,
    "campaign": SemanticAxis.CAMPAIGN,
    "ad_product": SemanticAxis.AD_PRODUCT,
    "marketplace": SemanticAxis.MARKETPLACE,
    "product": SemanticAxis.PRODUCT,
    "exposure_group": SemanticAxis.EXPOSURE_GROUP,
    "frequency_bucket": SemanticAxis.FREQUENCY_BUCKET,
    "audience_segment": SemanticAxis.AUDIENCE_SEGMENT,
    "identity_space": SemanticAxis.IDENTITY_SPACE,
    "attribution_model": SemanticAxis.ATTRIBUTION_MODEL,
    "acquisition_channel_group": SemanticAxis.ACQUISITION_CHANNEL_GROUP,
    "cohort": SemanticAxis.COHORT,
    "months_since_acquisition": SemanticAxis.MONTHS_SINCE_ACQUISITION,
}


_SELECTOR_AXES = {
    "attribution_model_key": SemanticAxis.ATTRIBUTION_MODEL,
    # A periodic metric's period grain is an exact selector on the time axis (D-7, ADR-100):
    # week and month rows coexist in one fact and nothing sums across them.
    PERIOD_GRAIN_SELECTOR: SemanticAxis.TIME,
    # A cohort row's repeat source (ADR-104): standard and ARP rows are separate deliveries on
    # separate bases, so a figure pins one, as it pins a source option.
    "cohort_path": SemanticAxis.SOURCE_OPTION,
    # A buyer profile's base (design §6.5): each profile path is its own delivery and base, so a
    # figure pins one and no series spans two.
    "profile_path": SemanticAxis.SOURCE_OPTION,
}

_PERIOD_GRAIN_VOCABULARY = (TimeGrain.WEEK, TimeGrain.MONTH, TimeGrain.QUARTER, TimeGrain.CUSTOM)
"""The `time_grain` selector's values: the complete periods a periodic fact serves, the calendar
grains and the cohort vintage (`custom`, ADR-104)."""
COHORT_PATHS = ("standard", "arp")
"""The `cohort_path` selector's values: pack 14's ad-touched repeats and pack 15's ARP repeats."""
PROFILE_PATHS = ("asi_in_market", "asi_lifestyle", "dsp_exposed_only")
"""The `profile_path` selector's values: packs 10 and 11's paid category tables, pack 12's DSP."""


@dataclass(frozen=True, slots=True)
class _PolicyRequest:
    grains: tuple[str, ...]
    defaults: dict[SemanticAxis, AxisPolicy]
    kind: AggregationKind
    selector_axes: frozenset[SemanticAxis]
    ecosystem_aggregation: AxisBehavior
    exact_axes: frozenset[SemanticAxis] = frozenset()


def _axis_aggregation(request: _PolicyRequest, axis: SemanticAxis) -> AxisBehavior:
    if request.kind is AggregationKind.EXACT_DISTINCT:
        return AxisBehavior.EXACT_ONLY
    if axis in NETWORK_AXES:
        return request.ecosystem_aggregation
    if axis in request.exact_axes:
        return AxisBehavior.EXACT_ONLY
    return request.defaults[axis].aggregation


_EXACT_ONLY_AXIS_GRAINS = frozenset(
    {
        "channel_group",
        "channel",
        "campaign",
        "ad_product",
        "product",
        # A shopper-aggregate sum is exact across every value of a disjoint axis inside one
        # fully returned delivery (D-3, D-9). The planner can't see whether every value came
        # back, so a metric declares these exact-only and the D-3 sum stays in domain code.
        "exposure_group",
        "frequency_bucket",
        "identity_space",
        # Overlapping, never summed (ADR-103 point 2): a sum metric declares it exact-only.
        "audience_segment",
        # Cohorts (design §6.6): a repeat sum is exact within one cohort cell; the D-9 sum across
        # a delivery's disjoint values is domain code, and follow-up months overlap.
        "acquisition_channel_group",
        "cohort",
        "months_since_acquisition",
    }
)
"""Media and product grains a metric may make `exact_only`; time, the network axes (owned by
`ecosystem_aggregation`) and marketplace (already `exact_only`) are not overridable."""


def _exact_only_axes(  # noqa: PLR0913, PLR0917 — one entry's declared shape
    raw: Mapping, kind: MetricKind, grains: tuple[str, ...], label: tuple[str, str]
) -> frozenset[SemanticAxis]:
    """Read `exact_only_axes`: axes whose figures one purchase may reach twice (design §6.2).

    A simple metric that credits one purchase to more than one channel or campaign can't be
    summed across those axes, so a request must group by them or pin them. Only a simple
    metric declares it: a ratio applies its operands' own rules.
    """
    where, name = label
    if "exact_only_axes" not in raw:
        return frozenset()
    if kind is not MetricKind.SIMPLE:
        raise CatalogError(f"{where}: {name} only a simple metric declares exact_only_axes.")
    declared = _as_str_tuple(
        raw["exact_only_axes"], key="exact_only_axes", where=f"{where}: {name}"
    )
    if (
        not declared
        or len(set(declared)) != len(declared)
        or not set(declared) <= _EXACT_ONLY_AXIS_GRAINS
        or not set(declared) <= set(grains)
    ):
        raise CatalogError(
            f"{where}: {name}.exact_only_axes must name distinct media or product grains the "
            "metric declares in valid_grains."
        )
    return frozenset(_GRAIN_AXES[grain] for grain in declared)


def _aggregation_policy(request: _PolicyRequest, label: tuple[str, str]) -> AggregationPolicy:
    grains = request.grains
    defaults = request.defaults
    axes = {_GRAIN_AXES[grain] for grain in grains} | set(request.selector_axes)
    policies = {
        axis: AxisPolicy(
            aggregation=_axis_aggregation(request, axis),
            groupable=defaults[axis].groupable,
            filterable=defaults[axis].filterable,
            alignable=defaults[axis].alignable,
        )
        for axis in axes
    }
    try:
        return AggregationPolicy(
            kind=request.kind,
            available_axes=frozenset(axes),
            axes=policies,
            ecosystem_aggregation=request.ecosystem_aggregation,
        )
    except SemanticCompatibilityError as error:
        where, name = label
        raise CatalogError(f"{where}: {name}: {error}") from None


_ECOSYSTEM_AGGREGATIONS = (AxisBehavior.ADDITIVE, AxisBehavior.EXACT_ONLY)


def _ecosystem_aggregation(raw: Mapping, label: tuple[str, str]) -> AxisBehavior:
    """Require the cross-network rule on every metric (ADR-106); a missing one fails loading."""
    where, name = label
    if "ecosystem_aggregation" not in raw:
        raise CatalogError(
            f"{where}: {name} requires ecosystem_aggregation: additive | exact_only, the rule "
            "for summing it across networks."
        )
    declared = _as_str(
        raw["ecosystem_aggregation"], key="ecosystem_aggregation", where=f"{where}: {name}"
    )
    for behavior in _ECOSYSTEM_AGGREGATIONS:
        if declared == behavior.value:
            return behavior
    raise CatalogError(
        f"{where}: {name}.ecosystem_aggregation is {declared!r}, expected additive or exact_only."
    )


def _axis_defaults(raw: Mapping, *, where: str) -> dict[SemanticAxis, AxisPolicy]:
    expected = set(_GRAIN_AXES.values()) | {SemanticAxis.SOURCE_OPTION}
    found: dict[SemanticAxis, AxisPolicy] = {}
    for name, value in raw.items():
        try:
            axis = SemanticAxis(name)
        except ValueError as error:
            raise CatalogError(f"{where}: unknown axis {name!r}.") from error
        spec = _as_mapping(value, where=f"{where}.{name}")
        if set(spec) != {"aggregation", "groupable", "filterable", "alignable"}:
            raise CatalogError(f"{where}.{name}: incomplete axis capabilities.")
        try:
            aggregation = AxisBehavior(
                _as_str(spec["aggregation"], key="aggregation", where=f"{where}.{name}")
            )
        except ValueError as error:
            raise CatalogError(f"{where}.{name}: unknown aggregation behavior.") from error
        found[axis] = AxisPolicy(
            aggregation=aggregation,
            groupable=_as_bool(spec["groupable"], key="groupable", where=f"{where}.{name}"),
            filterable=_as_bool(spec["filterable"], key="filterable", where=f"{where}.{name}"),
            alignable=_as_bool(spec["alignable"], key="alignable", where=f"{where}.{name}"),
        )
    if set(found) != expected:
        raise CatalogError(f"{where}: axis defaults must cover every governed query axis.")
    return found


@dataclass(frozen=True, slots=True)
class _ParseContext:
    """The file-wide values every entry needs, so parsing one takes three arguments."""

    units: dict[str, UnitKind]
    axis_defaults: dict[SemanticAxis, AxisPolicy]
    population: CommerceScopeKey
    business_calendar: BusinessCalendarKey
    profile_version: int
    selector_values: dict[str, frozenset[str]]
    where: str


_METRIC_KEYS = {
    "aliases",
    "label",
    "type",
    "aggregation",
    "time_class",
    "time_grain",
    "compatibility",
    "definition",
    "customer_definition",
    "customer_caveat",
    "units",
    "measurement_basis",
    "source_system",
    "source_datasets",
    "fee_inclusive",
    "valid_grains",
    "notes",
    "non_additive",
    "attribution_window_days",
    "includes_brand_halo",
    "lookback_months",
    "numerator",
    "denominator",
    "operands",
    "mixes_basis",
    "multiplier",
    "is_default_roas",
    "requires",
    "requires_selector",
    "ratio_scale",
    "favourable_direction",
    "ecosystem_aggregation",
    "exact_only_axes",
    "period_grains",
    "causal",
    "within_delivery",
    "unsupported_grain_reason",
    "cross_delivery",
    "delivery_ratio",
}
IDENTITY_SPACES = ("account", "device_or_browser")
"""The `identity_space` axis's values (ADR-101): Amazon account IDs, device or browser IDs. A
rate declares `requires: {identity_space: account}`, so it reads account IDs only (design §6.4)."""


def _declared_aggregation(
    raw: Mapping,
    kind: MetricKind,
    label: tuple[str, str],
) -> AggregationKind | None:
    where, name = label
    aggregation_name = _as_str(
        raw.get("aggregation", ""), key="aggregation", where=f"{where}: {name}"
    )
    if kind is MetricKind.RATIO:
        if aggregation_name != "ratio_of_operands":
            raise CatalogError(f"{where}: {name} ratio requires ratio_of_operands aggregation.")
        return None
    if kind is MetricKind.PRODUCT:
        if aggregation_name != "product_of_operands":
            raise CatalogError(f"{where}: {name} product requires product_of_operands aggregation.")
        return None
    try:
        aggregation_kind = AggregationKind(aggregation_name)
    except ValueError as error:
        raise CatalogError(
            f"{where}: {name} has unknown aggregation {aggregation_name!r}."
        ) from error
    if aggregation_kind not in {AggregationKind.SUM, AggregationKind.EXACT_DISTINCT}:
        raise CatalogError(f"{where}: {name} aggregation is not supported by this query pack.")
    return aggregation_kind


def _within_delivery(value: object, label: tuple[str, str]) -> WithinDelivery | None:
    """Read `within_delivery`: the cells a ratio's two sides take inside one delivery."""
    where, name = label
    if value is None:
        return None
    block = _as_mapping(value, where=f"{where}: {name}.within_delivery")
    if set(block) - {"per", "numerator", "denominator"}:
        raise CatalogError(f"{where}: {name}.within_delivery takes per, numerator, denominator.")

    def pins(side: str) -> tuple[tuple[str, str], ...]:
        raw = _as_mapping(block.get(side) or {}, where=f"{where}: {name}.within_delivery.{side}")
        found = tuple(
            (key, _as_str(item, key=f"within_delivery.{side}.{key}", where=f"{where}: {name}"))
            for key, item in sorted(raw.items())
        )
        unknown = sorted(key for key, _item in found if key not in _GRAIN_AXES)
        if unknown:
            raise CatalogError(f"{where}: {name}.within_delivery pins unknown grains {unknown}.")
        return found

    return WithinDelivery(
        numerator=pins("numerator"),
        denominator=pins("denominator"),
        per=_as_str_tuple(block.get("per") or [], key="within_delivery.per", where=where),
    )


def _delivery_ratio(raw: object, label: tuple[str, str]) -> DeliveryRatio | None:
    """Read `delivery_ratio`: a ratio of a row to its own delivery's total row (C2)."""
    if raw is None:
        return None
    where, name = label
    declared = _as_str(raw, key="delivery_ratio", where=f"{where}: {name}")
    try:
        return DeliveryRatio(declared)
    except ValueError:
        raise CatalogError(
            f"{where}: {name}.delivery_ratio is {declared!r}, expected segment_profile."
        ) from None


def _cross_delivery(raw: object, label: tuple[str, str]) -> CrossDeliveryException | None:
    """Read `cross_delivery`: the one declared D-8 (b) exception, the ARP cohort ratio."""
    if raw is None:
        return None
    where, name = label
    declared = _as_str(raw, key="cross_delivery", where=f"{where}: {name}")
    try:
        return CrossDeliveryException(declared)
    except ValueError:
        raise CatalogError(
            f"{where}: {name}.cross_delivery is {declared!r}, expected arp_cohort_ratio."
        ) from None


def _period_grains(raw: Mapping, label: tuple[str, str]) -> tuple[TimeGrain, ...]:
    """Read `period_grains`: the complete calendar periods a periodic metric is measured over."""
    where, name = label
    if "period_grains" not in raw:
        return ()
    declared = _as_str_tuple(raw["period_grains"], key="period_grains", where=f"{where}: {name}")
    vocabulary = {grain.value: grain for grain in _PERIOD_GRAIN_VOCABULARY}
    unknown = sorted(set(declared) - set(vocabulary))
    if unknown:
        raise CatalogError(
            f"{where}: {name}.period_grains names {', '.join(unknown)}; expected week, month, "
            "quarter or custom."
        )
    return tuple(vocabulary[grain] for grain in declared)


def _ratio_scale(raw: Mapping, unit_kind: UnitKind, label: tuple[str, str]) -> RatioScale | None:
    """Require `ratio_scale` on every ratio-unit metric and refuse it on any other unit."""
    where, name = label
    declared = raw.get("ratio_scale")
    if unit_kind is not UnitKind.RATIO:
        if declared is not None:
            raise CatalogError(f"{where}: {name} declares ratio_scale on a non-ratio unit.")
        return None
    if declared is None:
        raise CatalogError(f"{where}: {name} ratio unit requires ratio_scale: rate | multiplier.")
    try:
        return RatioScale(_as_str(declared, key="ratio_scale", where=f"{where}: {name}"))
    except ValueError:
        raise CatalogError(
            f"{where}: {name}.ratio_scale is {declared!r}, expected rate or multiplier."
        ) from None


def _favourable_direction(raw: Mapping, label: tuple[str, str]) -> FavourableDirection | None:
    """Read the optional ADR-121 field, refusing a value outside up, down and none."""
    where, name = label
    declared = raw.get("favourable_direction")
    if declared is None:
        return None
    try:
        return FavourableDirection(
            _as_str(declared, key="favourable_direction", where=f"{where}: {name}")
        )
    except ValueError:
        raise CatalogError(
            f"{where}: {name}.favourable_direction is {declared!r}, expected up, down or none."
        ) from None


def _check_kind_fields(raw: Mapping, kind: MetricKind, label: tuple[str, str]) -> None:
    """Refuse a declaration whose fields belong to another metric kind's shape."""
    where, name = label
    if kind is MetricKind.SIMPLE and any(key in raw for key in ("numerator", "denominator")):
        raise CatalogError(f"{where}: {name} simple metric cannot declare ratio operands.")
    if kind is MetricKind.RATIO and "definition" in raw:
        raise CatalogError(f"{where}: {name} ratio cannot declare a direct definition.")
    if kind is MetricKind.PRODUCT and any(
        key in raw for key in ("definition", "numerator", "denominator")
    ):
        raise CatalogError(f"{where}: {name} product declares only its factor operands.")
    if kind is not MetricKind.PRODUCT and "operands" in raw:
        raise CatalogError(f"{where}: {name} only a product metric declares operands.")


def _metric_from(name: str, raw: Mapping, context: _ParseContext) -> Metric:
    """Build one metric, naming the entry when anything is missing."""
    unknown = sorted(set(raw) - _METRIC_KEYS)
    if unknown:
        raise CatalogError(f"{context.where}: {name} has unknown fields {', '.join(unknown)}.")
    where = context.where
    units = context.units
    kind_name = _as_str(raw.get("type", ""), key="type", where=f"{where}: {name}")
    try:
        kind = MetricKind(kind_name)
    except ValueError as error:
        raise CatalogError(
            f"{where}: {name}.type is {kind_name!r}, expected one of "
            f"{', '.join(member.value for member in MetricKind)}."
        ) from error

    try:
        time_class = FactTimeClass(
            _as_str(
                raw.get("time_class", ""),
                key="time_class",
                where=f"{where}: {name}",
            )
        )
        time_grain = TimeGrain(
            _as_str(
                raw.get("time_grain", ""),
                key="time_grain",
                where=f"{where}: {name}",
            )
        )
        compatibility_mode = MetricCompatibilityMode(
            _as_str(
                raw.get("compatibility", ""),
                key="compatibility",
                where=f"{where}: {name}",
            )
        )
    except ValueError as error:
        raise CatalogError(
            f"{where}: {name} has unknown time or compatibility semantics."
        ) from error

    aggregation_kind = _declared_aggregation(raw, kind, (where, name))

    _check_kind_fields(raw, kind, (where, name))

    units_name = _as_str(raw.get("units", ""), key="units", where=f"{where}: {name}")
    unit_kind = units.get(units_name)
    if unit_kind is None:
        raise CatalogError(
            f"{where}: {name}.units is {units_name!r}, which the top-level units block does "
            f"not declare. Known: {', '.join(sorted(units))}."
        )

    ratio_scale = _ratio_scale(raw, unit_kind, (where, name))

    requires_raw = _as_mapping(raw.get("requires") or {}, where=f"{where}: {name}.requires")
    requires = tuple(
        (key, _as_str(value, key=f"requires.{key}", where=f"{where}: {name}"))
        for key, value in sorted(requires_raw.items())
    )
    for key, value in requires:
        if key not in context.selector_values or value not in context.selector_values[key]:
            raise CatalogError(f"{where}: {name} has unknown mandatory selector {key}={value}.")
    required_selector_names = _as_str_tuple(
        raw.get("requires_selector") or [],
        key="requires_selector",
        where=f"{where}: {name}",
    )
    period_grains = _period_grains(raw, (where, name))
    required_selectors: list[tuple[str, tuple[str, ...]]] = []
    for key in required_selector_names:
        values = context.selector_values.get(key)
        if values is None:
            raise CatalogError(f"{where}: {name} has unknown required selector {key}.")
        if key == PERIOD_GRAIN_SELECTOR and kind is MetricKind.SIMPLE:
            # One metric selects among its own declared grains, never the whole vocabulary. A
            # composite's grains are its periodic operands', set once every entry is read.
            values = frozenset(grain.value for grain in period_grains)
        required_selectors.append((key, tuple(sorted(values))))
    valid_grains = _as_str_tuple(
        raw.get("valid_grains") or [], key="valid_grains", where=f"{where}: {name}"
    )
    unknown_grains = sorted(set(valid_grains) - set(_GRAIN_AXES))
    if unknown_grains:
        raise CatalogError(f"{where}: {name} has unknown grains {', '.join(unknown_grains)}.")
    non_additive = _as_bool(
        raw.get("non_additive", False), key="non_additive", where=f"{where}: {name}"
    )
    if kind is MetricKind.SIMPLE and non_additive != (
        aggregation_kind is AggregationKind.EXACT_DISTINCT
    ):
        raise CatalogError(f"{where}: {name} non_additive contradicts declared aggregation.")

    halo = raw.get("includes_brand_halo")
    ecosystem_aggregation = _ecosystem_aggregation(raw, (where, name))
    exact_axes = _exact_only_axes(raw, kind, valid_grains, (where, name))

    return Metric(
        name=name,
        # Falling back to the name keeps a label-less entry usable. A label is presentation,
        # so its absence is a cosmetic defect rather than a reason to refuse the metric.
        label=_as_str(raw.get("label", name), key="label", where=f"{where}: {name}"),
        aliases=_as_str_tuple(raw.get("aliases", []), key="aliases", where=f"{where}: {name}"),
        kind=kind,
        unit_kind=unit_kind,
        measurement_basis=_bases(raw.get("measurement_basis") or [], name=name, where=where),
        valid_grains=valid_grains,
        time_class=time_class,
        time_grain=time_grain,
        compatibility_mode=compatibility_mode,
        population=context.population,
        business_calendar=context.business_calendar,
        profile_version=context.profile_version,
        definition=_as_str(raw.get("definition", ""), key="definition", where=f"{where}: {name}"),
        customer_definition=_as_str(
            raw.get("customer_definition", ""),
            key="customer_definition",
            where=f"{where}: {name}",
        ),
        customer_caveat=_as_str(
            raw.get("customer_caveat", ""),
            key="customer_caveat",
            where=f"{where}: {name}",
        ),
        numerator=_as_str(raw.get("numerator", ""), key="numerator", where=f"{where}: {name}"),
        denominator=_as_str(
            raw.get("denominator", ""), key="denominator", where=f"{where}: {name}"
        ),
        operands=_as_str_tuple(raw.get("operands") or [], key="operands", where=f"{where}: {name}"),
        notes=_as_str(raw.get("notes", ""), key="notes", where=f"{where}: {name}"),
        source_system=_as_str(
            raw.get("source_system", ""), key="source_system", where=f"{where}: {name}"
        ),
        source_datasets=_as_str_tuple(
            raw.get("source_datasets") or [], key="source_datasets", where=f"{where}: {name}"
        ),
        non_additive=non_additive,
        multiplier=_as_int(raw.get("multiplier", 1), key="multiplier", where=f"{where}: {name}"),
        declared_mixes_basis=_as_bool(
            raw.get("mixes_basis", False), key="mixes_basis", where=f"{where}: {name}"
        ),
        is_default_roas=_as_bool(
            raw.get("is_default_roas", False), key="is_default_roas", where=f"{where}: {name}"
        ),
        includes_brand_halo=(
            None
            if halo is None
            else _as_bool(halo, key="includes_brand_halo", where=f"{where}: {name}")
        ),
        requires=requires,
        required_selectors=tuple(required_selectors),
        ratio_scale=ratio_scale,
        favourable_direction=_favourable_direction(raw, (where, name)),
        ecosystem_aggregation=ecosystem_aggregation,
        period_grains=period_grains,
        aggregation_policy=(
            _aggregation_policy(
                _PolicyRequest(
                    grains=valid_grains,
                    defaults=context.axis_defaults,
                    kind=cast("AggregationKind", aggregation_kind),
                    selector_axes=frozenset(
                        {
                            *(SemanticAxis.SOURCE_OPTION for _key, _value in requires),
                            *(_SELECTOR_AXES[key] for key, _values in required_selectors),
                        }
                    ),
                    ecosystem_aggregation=ecosystem_aggregation,
                    exact_axes=exact_axes,
                ),
                (where, name),
            )
            if kind is MetricKind.SIMPLE
            else None
        ),
        attribution_window_days=_optional_int(
            raw.get("attribution_window_days"),
            key="attribution_window_days",
            where=f"{where}: {name}",
        ),
        lookback_months=_optional_int(
            raw.get("lookback_months"), key="lookback_months", where=f"{where}: {name}"
        ),
        fee_inclusive=(
            None
            if raw.get("fee_inclusive") is None
            else _as_bool(raw["fee_inclusive"], key="fee_inclusive", where=f"{where}: {name}")
        ),
        causal=(
            None
            if raw.get("causal") is None
            else _as_bool(raw["causal"], key="causal", where=f"{where}: {name}")
        ),
        within_delivery=_within_delivery(raw.get("within_delivery"), (where, name)),
        cross_delivery=_cross_delivery(raw.get("cross_delivery"), (where, name)),
        delivery_ratio=_delivery_ratio(raw.get("delivery_ratio"), (where, name)),
        unsupported_grain_reason=(
            None
            if raw.get("unsupported_grain_reason") is None
            else _as_str(
                raw["unsupported_grain_reason"],
                key="unsupported_grain_reason",
                where=f"{where}: {name}",
            )
        ),
    )


def _inventory_categories_from(value: object, *, where: str) -> tuple[InventoryCategory, ...]:
    if not isinstance(value, list):
        raise CatalogError(f"{where}: inventory_categories must be a list.")
    categories: list[InventoryCategory] = []
    for index, raw_category in enumerate(cast("list[object]", value)):
        category_where = f"{where}[{index}]"
        category = _as_mapping(raw_category, where=category_where)
        if set(category) != {"id", "label", "display_order", "metric_ids"}:
            raise CatalogError(f"{category_where}: inventory category has an invalid shape.")
        categories.append(
            InventoryCategory(
                category_id=_as_str(category["id"], key="id", where=category_where),
                label=_as_str(category["label"], key="label", where=category_where),
                display_order=_as_int(
                    category["display_order"], key="display_order", where=category_where
                ),
                metric_ids=_as_str_tuple(
                    category["metric_ids"], key="metric_ids", where=category_where
                ),
            )
        )
    if not categories:
        raise CatalogError(f"{where}: inventory_categories must not be empty.")
    return tuple(categories)


def _deferred_metrics_from(value: object, *, where: str) -> tuple[DeferredMetric, ...]:
    if not isinstance(value, list):
        raise CatalogError(f"{where}: deferred must be a list.")
    deferred: list[DeferredMetric] = []
    for index, raw_metric in enumerate(cast("list[object]", value)):
        metric_where = f"{where}[{index}]"
        metric = _as_mapping(raw_metric, where=metric_where)
        required = {"metric_id", "label", "kind", "unit"}
        if not required <= set(metric) or not set(metric) <= required | {"aliases"}:
            raise CatalogError(f"{metric_where}: deferred metric has an invalid shape.")
        try:
            kind = MetricKind(_as_str(metric["kind"], key="kind", where=metric_where))
            unit_kind = UnitKind(_as_str(metric["unit"], key="unit", where=metric_where))
        except ValueError as error:
            raise CatalogError(
                f"{metric_where}: deferred metric has an unknown kind or unit."
            ) from error
        deferred.append(
            DeferredMetric(
                name=_as_str(metric["metric_id"], key="metric_id", where=metric_where),
                label=_as_str(metric["label"], key="label", where=metric_where),
                kind=kind,
                unit_kind=unit_kind,
                aliases=(
                    _as_str_tuple(metric["aliases"], key="aliases", where=metric_where)
                    if "aliases" in metric
                    else ()
                ),
            )
        )
    return tuple(deferred)


def load_catalog(root: Path) -> Catalog:
    """Read the metric dictionary into a `Catalog`.

    Takes the repository root rather than a full path, matching `load_contracts` and
    `load_registry`, so a caller states where the repository is and not where inside it each
    reference file lives.
    """
    path = root / DICTIONARY_PATH
    if not path.exists():
        # The dictionary ships with the code, so its absence means the deployment is broken
        # rather than that a caller asked for something unavailable.
        raise CatalogError(f"the metric dictionary is missing at {path}.")

    where = str(path)
    text = path.read_text()
    composed = cast(
        "object",
        yaml.compose(text, Loader=yaml.SafeLoader),  # type: ignore[reportUnknownMemberType]
    )
    if composed is not None and not isinstance(composed, yaml.Node):
        raise CatalogError(f"{where}: parser returned an invalid YAML node.")
    _reject_duplicate_keys(composed, where)
    raw = _as_mapping(cast("object", yaml.safe_load(text)), where=where)
    if set(raw) != {
        "version",
        "axis_defaults",
        "compatibility_defaults",
        "units",
        "inventory_categories",
        "metrics",
        "deferred",
    }:
        raise CatalogError(
            f"{where}: expected version, axis_defaults, compatibility_defaults, units, "
            "inventory_categories, metrics and deferred."
        )
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise CatalogError(f"{where}: version must be integer 1.")
    axes = _axis_defaults(
        _as_mapping(raw["axis_defaults"], where=f"{where}: axis_defaults"),
        where=f"{where}: axis_defaults",
    )
    compatibility = _as_mapping(
        raw["compatibility_defaults"], where=f"{where}: compatibility_defaults"
    )
    if set(compatibility) != {
        "population",
        "business_calendar",
        "week_starts_on",
        "profile_version",
    }:
        raise CatalogError(f"{where}: compatibility_defaults has an invalid shape.")
    population = CommerceScopeKey(
        _as_str(compatibility["population"], key="population", where=where)
    )
    calendar = BusinessCalendarKey(
        _as_str(compatibility["business_calendar"], key="business_calendar", where=where),
        week_starts_on=_as_int(compatibility["week_starts_on"], key="week_starts_on", where=where),
    )
    profile_version = _as_int(compatibility["profile_version"], key="profile_version", where=where)
    if profile_version < 1:
        raise CatalogError(f"{where}: profile_version starts at 1.")
    units = _unit_kinds(_as_mapping(raw.get("units") or {}, where=f"{where}: units"), where=where)

    metrics_raw = _as_mapping(raw.get("metrics") or {}, where=f"{where}: metrics")
    if not metrics_raw:
        raise CatalogError(f"{where}: declares no metrics, so nothing can be asked for.")

    selector_values = {
        name: frozenset(definition.values) for name, definition in REPORT_LENSES.items()
    }
    if (root / ATTRIBUTION_MODELS_PATH).is_file():
        selector_values["attribution_model_key"] = frozenset(load_attribution_models(root).keys)
    selector_values[PERIOD_GRAIN_SELECTOR] = frozenset(
        grain.value for grain in _PERIOD_GRAIN_VOCABULARY
    )
    selector_values["identity_space"] = frozenset(IDENTITY_SPACES)
    selector_values["cohort_path"] = frozenset(COHORT_PATHS)
    selector_values["profile_path"] = frozenset(PROFILE_PATHS)
    context = _ParseContext(
        units=units,
        axis_defaults=axes,
        population=population,
        business_calendar=calendar,
        profile_version=profile_version,
        selector_values=selector_values,
        where=where,
    )
    metrics = _with_operand_period_selectors(
        tuple(
            _metric_from(name, _as_mapping(entry, where=f"{where}: metrics.{name}"), context)
            for name, entry in metrics_raw.items()
        ),
        where,
    )
    deferred = _deferred_metrics_from(raw["deferred"], where=f"{where}: deferred")
    categories = _inventory_categories_from(
        raw["inventory_categories"], where=f"{where}: inventory_categories"
    )
    declared = Catalog(metrics=metrics, deferred=deferred, inventory_categories=categories)
    # ADR-106: the platforms each simple metric can come from, derived in the domain from the
    # contracts, source packs and media taxonomy. This adapter only loads the files.
    derived = derive_source_platforms(
        SourcePlatformsRequest(
            catalog=declared,
            contracts=tuple(contract for _, contract in load_contracts(root)),
            source_packs=load_source_packs(root),
            taxonomy=load_media_taxonomy(root),
        )
    ).platforms
    return Catalog(
        metrics=tuple(
            replace(metric, source_platforms=derived[metric.name])
            if metric.name in derived
            else metric
            for metric in metrics
        ),
        deferred=deferred,
        inventory_categories=categories,
    )


def _with_operand_period_selectors(metrics: tuple[Metric, ...], where: str) -> tuple[Metric, ...]:
    """Narrow a composite's `time_grain` selector to the grains its periodic operands share.

    A ratio over a periodic count is served at that count's periods only (D-6, D-7), so its
    selector offers exactly the grains every periodic operand declares, and none when it reads
    no periodic operand, which refuses loading.
    """
    by_name = {metric.name: metric for metric in metrics}

    def leaves(metric: Metric, seen: frozenset[str]) -> tuple[Metric, ...]:
        if metric.kind is MetricKind.SIMPLE:
            return (metric,)
        names = (metric.numerator, metric.denominator, *metric.operands)
        return tuple(
            leaf
            for name in names
            if name and name not in seen and name in by_name
            for leaf in leaves(by_name[name], seen | {name})
        )

    narrowed: list[Metric] = []
    for metric in metrics:
        selectors = dict(metric.required_selectors)
        if metric.kind is MetricKind.SIMPLE or PERIOD_GRAIN_SELECTOR not in selectors:
            narrowed.append(metric)
            continue
        periodic = [
            leaf
            for leaf in leaves(metric, frozenset({metric.name}))
            if leaf.time_class is FactTimeClass.PERIODIC
        ]
        shared: set[str] = (
            set[str].intersection(
                *({grain.value for grain in leaf.period_grains} for leaf in periodic)
            )
            if periodic
            else set()
        )
        if not shared:
            raise CatalogError(
                f"{where}: {metric.name} requires {PERIOD_GRAIN_SELECTOR} but reads no periodic "
                "operand whose grains it could select."
            )
        selectors[PERIOD_GRAIN_SELECTOR] = tuple(sorted(shared))
        narrowed.append(
            replace(
                metric,
                required_selectors=tuple(
                    (key, selectors[key]) for key, _values in metric.required_selectors
                ),
            )
        )
    return tuple(narrowed)


def _reject_duplicate_keys(node: yaml.Node | None, where: str) -> None:
    if node is None:
        return
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in seen:
                raise CatalogError(f"{where}: duplicate YAML key {key!r}.")
            seen.add(key)
            _reject_duplicate_keys(value_node, where)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _reject_duplicate_keys(child, where)
