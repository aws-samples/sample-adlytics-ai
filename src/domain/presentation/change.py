# SPDX-License-Identifier: MIT-0
"""Server-declared change semantics for every served comparison (ADR-121).

A comparison's `change` block says which way the value moved, whether that is good news, in
which unit the change reads, and the exact text the change chip shows. The browser renders the
block and never infers a direction or a tone from a sign.

Four rules live here:

- **Tone is governed.** `sentiment` comes from the metric dictionary's `favourable_direction`,
  never from the sign: a fall in cost per click is favourable, and spend is always neutral.
- **"No change" means equal unrounded values.** Every change is computed from the unrounded
  operands, and a real change that is smaller than the display step reads "<0.1%",
  "<0.001 pts" or "<0.01" and the times sign with its direction, never "No change".
- **A percentage is shown only where it means something.** A prior of zero reads "New", a
  prior loss reads "Up from a loss" or "Down from a loss", and a prior so small that the move
  is ten or more times its size reads as the absolute change.
- **The unit follows the metric's scale.** Counts and money change in percent, a rate (CTR,
  return rate) in percentage points, and a multiplier (ROAS) in times.

The decoders in the artifact modules call `change_from_payload`, which refuses a block the
server could not have produced from the same operands. The favourable direction a block
implies is the only thing it carries that its operands do not, so the decoders read it back
from `direction` and `sentiment` rather than taking the metric dictionary as a parameter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal
from enum import StrEnum
from typing import cast

from domain.catalog.capability import Catalog, CatalogError, FavourableDirection, RatioScale
from domain.narrative.shaped_response import UnitKind, Units

__all__ = [
    "CHANGE_KEY",
    "NEAR_ZERO_PRIOR_FACTOR",
    "PERCENTAGE_KEY",
    "Change",
    "ChangeDirection",
    "ChangeOperands",
    "ChangeScale",
    "ChangeSemantics",
    "ChangeSentiment",
    "ChangeUnit",
    "ComparedMetric",
    "NotMeaningful",
    "build_change",
    "change_from_payload",
    "change_payload",
    "change_semantics_from",
    "check_change",
    "comparison_change_payload",
    "comparison_fields",
    "has_change_key",
    "require_change_blocks",
]

CHANGE_KEY = "change"

NEAR_ZERO_PRIOR_FACTOR = Decimal(10)
"""A prior is near zero when the move is at least this many times its size (1,000% or more)."""

_CONTEXT = Context(prec=80)
"""Wide enough that a decimal(38,4) quotient keeps every digit the display step can see."""

_MINUS = "\u2212"
_PERCENT_PLACES = 1
_RATE_PLACES_UNDER_ONE_PERCENT = 3
_RATE_PLACES = 1
_RATE_SMALL = Decimal("0.01")
"""A rate under 1% (CTR) shows three decimals, so its change in points shows three too."""
_TIMES_PLACES = 2
_CURRENCY_PLACES = 2
_NO_CHANGE = "No change"
_TIMES = "\u00d7"
"""The multiplication sign a multiplier's change ends with, as in "+0.21" + _TIMES."""
_PAYLOAD_KEYS = frozenset({"direction", "sentiment", "unit", "display_text", "not_meaningful"})

type ChangeOperand = Decimal | int | str


class ChangeDirection(StrEnum):
    """Which way the unrounded value moved."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class ChangeSentiment(StrEnum):
    """Whether the move is good news, from the metric's declared favourable direction."""

    FAVOURABLE = "favourable"
    UNFAVOURABLE = "unfavourable"
    NEUTRAL = "neutral"


class ChangeUnit(StrEnum):
    """What the chip's number counts."""

    PERCENT = "percent"
    PERCENTAGE_POINTS = "percentage_points"
    ABSOLUTE = "absolute"


class NotMeaningful(StrEnum):
    """Why a percentage would mislead, so the chip shows a word or an absolute change."""

    NEAR_ZERO_PRIOR = "near_zero_prior"
    NEGATIVE_PRIOR = "negative_prior"
    NO_PRIOR = "no_prior"


class ChangeScale(StrEnum):
    """How a metric's change reads, from its dictionary units and ratio scale."""

    RELATIVE = "relative"
    """Counts and money: percent of the prior."""
    POINTS = "points"
    """A rate: percentage points."""
    TIMES = "times"
    """A multiplier: the absolute change in times."""


@dataclass(frozen=True, slots=True)
class ComparedMetric:
    """The two governed facts one metric's change needs."""

    favourable: FavourableDirection
    scale: ChangeScale


@dataclass(frozen=True, slots=True)
class ChangeSemantics:
    """The compared metrics' governed directions and scales, keyed by dictionary metric id."""

    metrics: tuple[tuple[str, ComparedMetric], ...]

    def __post_init__(self) -> None:
        names = tuple(name for name, _metric in self.metrics)
        if len(set(names)) != len(names):
            raise ValueError("change semantics name each metric once.")

    def for_metric(self, metric_id: str) -> ComparedMetric:
        """Return one compared metric's semantics, refusing a metric the roster lacks."""
        for name, metric in self.metrics:
            if name == metric_id:
                return metric
        raise ValueError(f"{metric_id} has no declared change semantics.")


def _scale_of(unit_kind: UnitKind, ratio_scale: RatioScale | None) -> ChangeScale:
    if unit_kind is not UnitKind.RATIO:
        return ChangeScale.RELATIVE
    return ChangeScale.POINTS if ratio_scale is RatioScale.RATE else ChangeScale.TIMES


def change_semantics_from(
    catalog: Catalog,
    compared: Iterable[str],
    *,
    relative: frozenset[str] = frozenset(),
) -> ChangeSemantics:
    """Load the compared roster's semantics, refusing a metric without `favourable_direction`.

    Every missing or unknown metric is named in one error, so a reviewer adds them together.
    `relative` names rates a page shows rescaled rather than as a percentage, whose change
    therefore reads in percent, never in points of an unshown percentage.
    """
    unknown: list[str] = []
    missing: list[str] = []
    resolved: list[tuple[str, ComparedMetric]] = []
    for name in sorted(set(compared)):
        metric = catalog.metric(name)
        if metric is None:
            unknown.append(name)
        elif metric.favourable_direction is None:
            missing.append(name)
        else:
            scale = (
                ChangeScale.RELATIVE
                if name in relative
                else _scale_of(metric.unit_kind, metric.ratio_scale)
            )
            resolved.append((name, ComparedMetric(metric.favourable_direction, scale)))
    if unknown:
        raise CatalogError(f"compared metrics are not declared: {', '.join(unknown)}.")
    if missing:
        raise CatalogError(
            "every metric served with a comparison declares favourable_direction (ADR-121); "
            f"missing on: {', '.join(missing)}."
        )
    return ChangeSemantics(tuple(resolved))


@dataclass(frozen=True, slots=True)
class Change:
    """One comparison's server-declared change: the chip renders exactly these fields."""

    direction: ChangeDirection
    sentiment: ChangeSentiment
    unit: ChangeUnit
    display_text: str
    not_meaningful: NotMeaningful | None

    def __post_init__(self) -> None:
        if not self.display_text.strip():
            raise ValueError("a change states its display text.")
        if self.direction is ChangeDirection.FLAT and (
            self.sentiment is not ChangeSentiment.NEUTRAL or self.not_meaningful is not None
        ):
            raise ValueError("an unchanged value is neutral and meaningful.")


def _sentiment(direction: ChangeDirection, favourable: FavourableDirection) -> ChangeSentiment:
    if direction is ChangeDirection.FLAT or favourable is FavourableDirection.NONE:
        return ChangeSentiment.NEUTRAL
    if direction.value == favourable.value:
        return ChangeSentiment.FAVOURABLE
    return ChangeSentiment.UNFAVOURABLE


def _rounded(magnitude: Decimal, places: int) -> Decimal:
    step = Decimal(1).scaleb(-places)
    return magnitude.quantize(step, rounding=ROUND_HALF_UP, context=_CONTEXT)


def _step_text(places: int) -> str:
    return format(Decimal(1).scaleb(-places), "f")


def _signed(text: str, direction: ChangeDirection) -> str:
    return f"{'+' if direction is ChangeDirection.UP else _MINUS}{text}"


@dataclass(frozen=True, slots=True)
class _Step:
    """A display precision and what follows the number: "%", " pts" or the times sign."""

    places: int
    suffix: str


def _magnitude_text(magnitude: Decimal, step: _Step, direction: ChangeDirection) -> str:
    """Write a rounded magnitude with its sign, or "<step" when it rounds to zero."""
    rounded = _rounded(magnitude, step.places)
    if rounded.is_zero():
        return f"<{_step_text(step.places)}{step.suffix}"
    return _signed(f"{format(rounded, ',f')}{step.suffix}", direction)


@dataclass(frozen=True, slots=True)
class ChangeOperands:
    """The two unrounded sides of one comparison and the units they share."""

    current: ChangeOperand
    prior: ChangeOperand
    units: Units

    @property
    def now(self) -> Decimal:
        """The current side as a decimal."""
        return Decimal(self.current)

    @property
    def before(self) -> Decimal:
        """The prior side as a decimal."""
        return Decimal(self.prior)


def _absolute_text(operands: ChangeOperands, direction: ChangeDirection) -> str:
    magnitude = abs(operands.now - operands.before)
    if operands.units.kind is UnitKind.CURRENCY:
        code = operands.units.currency_code
        step = _Step(_CURRENCY_PLACES, "" if code is None else f" {code}")
        return _magnitude_text(magnitude, step, direction)
    places = 0 if magnitude == magnitude.to_integral_value() else _CURRENCY_PLACES
    return _magnitude_text(magnitude, _Step(places, ""), direction)


type _Reading = tuple[ChangeUnit, str, NotMeaningful | None]


def _relative(operands: ChangeOperands, direction: ChangeDirection) -> _Reading:
    prior = operands.before
    delta = operands.now - prior
    if prior.is_zero():
        word = "New" if direction is ChangeDirection.UP else "Down from zero"
        return ChangeUnit.ABSOLUTE, word, NotMeaningful.NO_PRIOR
    if prior < 0:
        word = "Up from a loss" if direction is ChangeDirection.UP else "Down from a loss"
        return ChangeUnit.ABSOLUTE, word, NotMeaningful.NEGATIVE_PRIOR
    if abs(delta) >= NEAR_ZERO_PRIOR_FACTOR * prior:
        return (
            ChangeUnit.ABSOLUTE,
            _absolute_text(operands, direction),
            NotMeaningful.NEAR_ZERO_PRIOR,
        )
    magnitude = _CONTEXT.multiply(_CONTEXT.divide(abs(delta), prior), Decimal(100))
    text = _magnitude_text(magnitude, _Step(_PERCENT_PLACES, "%"), direction)
    return ChangeUnit.PERCENT, text, None


def _points(operands: ChangeOperands, direction: ChangeDirection) -> _Reading:
    now, before = operands.now, operands.before
    small = max(abs(now), abs(before)) < _RATE_SMALL
    places = _RATE_PLACES_UNDER_ONE_PERCENT if small else _RATE_PLACES
    magnitude = _CONTEXT.multiply(abs(now - before), Decimal(100))
    text = _magnitude_text(magnitude, _Step(places, " pts"), direction)
    return ChangeUnit.PERCENTAGE_POINTS, text, None


def _times(operands: ChangeOperands, direction: ChangeDirection) -> _Reading:
    magnitude = abs(operands.now - operands.before)
    text = _magnitude_text(magnitude, _Step(_TIMES_PLACES, _TIMES), direction)
    return ChangeUnit.ABSOLUTE, text, None


_FLAT_UNITS = {
    ChangeScale.RELATIVE: ChangeUnit.PERCENT,
    ChangeScale.POINTS: ChangeUnit.PERCENTAGE_POINTS,
    ChangeScale.TIMES: ChangeUnit.ABSOLUTE,
}


def build_change(operands: ChangeOperands, metric: ComparedMetric) -> Change:
    """Build one change from unrounded operands and the metric's governed semantics."""
    delta = operands.now - operands.before
    direction = (
        ChangeDirection.FLAT
        if delta.is_zero()
        else ChangeDirection.UP
        if delta > 0
        else ChangeDirection.DOWN
    )
    if direction is ChangeDirection.FLAT:
        return Change(
            direction, ChangeSentiment.NEUTRAL, _FLAT_UNITS[metric.scale], _NO_CHANGE, None
        )
    if metric.scale is ChangeScale.RELATIVE:
        unit, text, not_meaningful = _relative(operands, direction)
    elif metric.scale is ChangeScale.POINTS:
        unit, text, not_meaningful = _points(operands, direction)
    else:
        unit, text, not_meaningful = _times(operands, direction)
    return Change(direction, _sentiment(direction, metric.favourable), unit, text, not_meaningful)


def _implied_favourable(change: Change) -> FavourableDirection:
    """Read back the one governed fact a block carries beyond its operands."""
    if change.sentiment is ChangeSentiment.NEUTRAL or change.direction is ChangeDirection.FLAT:
        return FavourableDirection.NONE
    if change.sentiment is ChangeSentiment.FAVOURABLE:
        return FavourableDirection(change.direction.value)
    return (
        FavourableDirection.DOWN
        if change.direction is ChangeDirection.UP
        else FavourableDirection.UP
    )


def _implied_scale(change: Change, unit_kind: UnitKind) -> ChangeScale:
    if unit_kind is not UnitKind.RATIO:
        return ChangeScale.RELATIVE
    if change.unit is ChangeUnit.PERCENTAGE_POINTS:
        return ChangeScale.POINTS
    # A rescaled rate changes in percent, and falls back to a word or absolute change exactly
    # as a count does; a multiplier's absolute change in times is always meaningful.
    if change.unit is ChangeUnit.PERCENT or change.not_meaningful is not None:
        return ChangeScale.RELATIVE
    return ChangeScale.TIMES


def check_change(change: Change, operands: ChangeOperands) -> None:
    """Refuse a block the server could not have produced from these unrounded operands."""
    metric = ComparedMetric(
        _implied_favourable(change), _implied_scale(change, operands.units.kind)
    )
    if build_change(operands, metric) != change:
        raise ValueError("a change block does not match its unrounded operands.")


def change_payload(change: Change | None) -> dict[str, str | None] | None:
    """Encode one change block, or `None` where no change exists."""
    if change is None:
        return None
    return {
        "direction": change.direction.value,
        "sentiment": change.sentiment.value,
        "unit": change.unit.value,
        "display_text": change.display_text,
        "not_meaningful": None if change.not_meaningful is None else change.not_meaningful.value,
    }


def _enum[E: StrEnum](kind: type[E], value: object, label: str) -> E:
    if not isinstance(value, str):
        raise ValueError(f"{label} is not text.")
    try:
        return kind(value)
    except ValueError:
        raise ValueError(f"{label} is unsupported.") from None


def change_from_payload(value: object, operands: ChangeOperands, label: str) -> Change | None:
    """Decode one block and refuse it unless its operands reproduce it exactly."""
    if value is None:
        return None
    if not isinstance(value, dict) or frozenset(cast("dict[str, object]", value)) != _PAYLOAD_KEYS:
        raise ValueError(f"{label} change has an invalid shape.")
    item = cast("dict[str, object]", value)
    text = item["display_text"]
    if not isinstance(text, str):
        raise ValueError(f"{label} change text is invalid.")
    not_meaningful = item["not_meaningful"]
    change = Change(
        _enum(ChangeDirection, item["direction"], f"{label} change direction"),
        _enum(ChangeSentiment, item["sentiment"], f"{label} change sentiment"),
        _enum(ChangeUnit, item["unit"], f"{label} change unit"),
        text,
        None
        if not_meaningful is None
        else _enum(NotMeaningful, not_meaningful, f"{label} change state"),
    )
    check_change(change, operands)
    return change


PERCENTAGE_KEY = "percentage_change"


def comparison_change_payload(percentage: str | None, change: Change | None) -> dict[str, object]:
    """Encode a comparison's change fields: the block, or the percentage beside `change: null`.

    A change block replaces the percentage the chip used to read, so a comparison with a block
    no longer writes `percentage_change` (the block's text is what renders, and the 50-row
    served campaign body stays under its byte cap). A comparison without one (an undefined
    side, or a withheld change) keeps the percentage beside `change: null`.
    """
    if change is None:
        return {PERCENTAGE_KEY: percentage, CHANGE_KEY: None}
    return {CHANGE_KEY: change_payload(change)}


def comparison_fields(value: object, base: frozenset[str]) -> frozenset[str]:
    """Return the exact key set a comparison carries, from `base` (without the change fields).

    Every comparison carries the `change` key: the block alone, or `percentage_change` beside
    `change: null` when no change exists. A comparison without the key, the form baked before
    change blocks, is refused: that retained decode retired in the integration change, because
    the API, a full re-bake and the browser deploy together (owner revision, 2026-10-08).
    """
    if not has_change_key(value):
        raise ValueError("a comparison without a change key is not a current body.")
    if cast("dict[str, object]", value)[CHANGE_KEY] is None:
        return base | {PERCENTAGE_KEY, CHANGE_KEY}
    return base | {CHANGE_KEY}


def has_change_key(value: object) -> bool:
    """Return whether one decoded object carries the `change` key itself."""
    return isinstance(value, dict) and CHANGE_KEY in cast("dict[str, object]", value)


def require_change_blocks(changes: Iterable[Change | None], label: str) -> None:
    """Refuse a body where a computable comparison carries no change block.

    Every comparison whose two sides exist and whose change isn't withheld carries one. A body
    with none, the retained form baked before the render-format bump, is refused like a mixed
    one: there is no retained read (owner revision, 2026-10-08).
    """
    if not all(change is not None for change in changes):
        raise ValueError(f"{label} carries a computable comparison with no change block.")
