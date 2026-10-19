# SPDX-License-Identifier: MIT-0
"""Deterministic attribution-model robustness artifact for Amazon channel performance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast

from domain.narrative.shaped_response import ShapedResponse, UnitKind
from domain.presentation.analytical_pages import (
    AnalyticalPageResult,
    AvailabilityState,
    PageMeasurementResult,
    PresentationDimensionLabel,
)
from domain.shared_kernel.values import DateRange

SPEND_METRIC = "ad_spend"
ROAS_METRIC = "roas_deduplicated_incl_halo_14d"
MODEL_IDS = ("first_touch_v1", "last_touch_v1", "linear_v1")
BOUNDARIES = (
    (
        "ROAS uses deduplicated attributed revenue including brand halo over a 14-day "
        "attribution window."
    ),
    (
        "Attributed revenue uses conversion date while spend uses delivery date in the "
        "requested period."
    ),
    (
        "Agreement across attribution models is descriptive sensitivity evidence, not "
        "incrementality or causal budget guidance."
    ),
)


class ChannelRobustnessOutcome(StrEnum):
    """Closed deterministic page states."""

    ROBUST_LEADER = "robust_leader"
    MODEL_SENSITIVE = "model_sensitive"
    INSUFFICIENT = "insufficient"


class ChannelInsufficiencyReason(StrEnum):
    """Closed evidence failures that forbid a winner conclusion."""

    EMPTY = "empty"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    MISSING_PUBLICATION = "missing_publication"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    MISSING_VALUE = "missing_value"
    MULTIPLE_MARKETPLACES = "multiple_marketplaces"


@dataclass(frozen=True, slots=True)
class ChannelModelResult:
    """One exact model's governed ROAS value, rank, and evidence state."""

    model_id: str
    value: Decimal | None
    rank: int | None
    evidence: AvailabilityState

    def __post_init__(self) -> None:
        if self.model_id not in MODEL_IDS:
            raise ValueError("channel robustness model id is not registered.")
        if (self.value is None) != (self.rank is None):
            raise ValueError("channel robustness value and rank must be present together.")
        if self.rank is not None and self.rank < 1:
            raise ValueError("channel robustness rank starts at one.")


@dataclass(frozen=True, slots=True)
class ChannelRobustnessRow:
    """One canonical channel aligned across spend and every registered model."""

    channel_key: str
    spend: Decimal | None
    model_results: tuple[ChannelModelResult, ...]

    def __post_init__(self) -> None:
        if not self.channel_key.strip():
            raise ValueError("channel robustness row requires a canonical channel key.")
        if tuple(result.model_id for result in self.model_results) != MODEL_IDS:
            raise ValueError("channel robustness row must preserve the registered model order.")


@dataclass(frozen=True, slots=True)
class ModelLeaders:
    """Rank-one canonical channels for one exact model, including ties."""

    model_id: str
    channel_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.model_id not in MODEL_IDS or len(set(self.channel_keys)) != len(self.channel_keys):
            raise ValueError("model leaders require one registered model and unique channels.")


@dataclass(frozen=True, slots=True)
class ChannelAttributionRobustness:
    """Closed page artifact; narration may explain it but cannot alter it."""

    outcome: ChannelRobustnessOutcome
    period: DateRange
    marketplace_key: str | None
    currency: str | None
    rows: tuple[ChannelRobustnessRow, ...]
    leaders_by_model: tuple[ModelLeaders, ...]
    agreed_leader_key: str | None
    insufficiency_reasons: tuple[ChannelInsufficiencyReason, ...]
    boundaries: tuple[str, ...]
    qualifiers: tuple[str, ...]

    def __post_init__(self) -> None:
        if tuple(item.model_id for item in self.leaders_by_model) != MODEL_IDS:
            raise ValueError("channel robustness leaders must preserve registered model order.")
        if self.boundaries != BOUNDARIES:
            raise ValueError("channel robustness boundaries are fixed domain policy.")
        insufficient = self.outcome is ChannelRobustnessOutcome.INSUFFICIENT
        if insufficient != bool(self.insufficiency_reasons):
            raise ValueError("only insufficient channel robustness carries insufficiency reasons.")
        if (self.agreed_leader_key is not None) != (
            self.outcome is ChannelRobustnessOutcome.ROBUST_LEADER
        ):
            raise ValueError("only robust leader outcome carries one agreed leader.")


@dataclass(frozen=True, slots=True)
class ChannelRobustnessRequest:
    """Exact period evidence, including stable publication absence when no page exists."""

    page: AnalyticalPageResult | None
    period: DateRange
    missing_publication: bool = False

    def __post_init__(self) -> None:
        if self.missing_publication != (self.page is None):
            raise ValueError(
                "channel robustness requires either a complete page or missing publication."
            )
        if self.page is not None and self.page.schema.page_id != "amazon-channel-performance":
            raise ValueError("channel robustness requires the governed channel page schema.")


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        return None
    return Decimal(value)


def _rows(answer: ShapedResponse) -> dict[tuple[str, str], Decimal | None]:
    result: dict[tuple[str, str], Decimal | None] = {}
    for row in answer.data.values:
        channel = row.get("channel_key")
        marketplace = row.get("marketplace_key")
        if not isinstance(channel, str) or not isinstance(marketplace, str):
            continue
        key = (marketplace, channel)
        if key in result:
            return {}
        result[key] = _decimal(row.get("value"))
    return result


def _availability_reasons(
    measurements: tuple[PageMeasurementResult, ...],
) -> set[ChannelInsufficiencyReason]:
    reasons: set[ChannelInsufficiencyReason] = set()
    for measurement in measurements:
        if measurement.availability is AvailabilityState.EMPTY:
            reasons.add(ChannelInsufficiencyReason.EMPTY)
        elif measurement.availability is AvailabilityState.SUPPRESSED:
            reasons.add(ChannelInsufficiencyReason.SUPPRESSED)
        elif measurement.availability is AvailabilityState.PARTIAL:
            reasons.add(ChannelInsufficiencyReason.TRUNCATED)
    return reasons


def _compatible(measurements: tuple[PageMeasurementResult, ...], period: DateRange) -> bool:
    answers = tuple(measurement.answer for measurement in measurements)
    first = answers[0]
    return bool(first.metadata.actual_coverage_intervals) and all(
        answer.data.grain == first.data.grain
        and answer.metadata.requested_coverage_intervals == (period,)
        and answer.metadata.actual_coverage_intervals == first.metadata.actual_coverage_intervals
        and not answer.metadata.coverage_holes
        and answer.metadata.source_timezone == first.metadata.source_timezone
        for answer in answers
    )


def _ranked(
    model_id: str,
    values: dict[str, Decimal],
) -> tuple[dict[str, int], ModelLeaders]:
    distinct = sorted(set(values.values()), reverse=True)
    ranks = {channel: distinct.index(value) + 1 for channel, value in values.items()}
    leaders = tuple(sorted(channel for channel, rank in ranks.items() if rank == 1))
    return ranks, ModelLeaders(model_id, leaders)


def _qualifiers(measurements: tuple[PageMeasurementResult, ...]) -> tuple[str, ...]:
    values = {
        qualifier.rendering
        for measurement in measurements
        for qualifier in measurement.answer.metadata.qualifiers
    }
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class _AlignedEvidence:
    reasons: frozenset[ChannelInsufficiencyReason]
    currency: str | None
    marketplace: str | None
    spend_rows: dict[tuple[str, str], Decimal | None]
    model_rows: tuple[dict[tuple[str, str], Decimal | None], ...]
    key_set: frozenset[tuple[str, str]]


def _aligned_evidence(
    measurements: tuple[PageMeasurementResult, ...], period: DateRange
) -> _AlignedEvidence:
    reasons = _availability_reasons(measurements)
    if not _compatible(measurements, period):
        reasons.add(ChannelInsufficiencyReason.INCOMPATIBLE_SCOPE)
    spend = measurements[0]
    models = measurements[1:]
    if (
        spend.answer.data.units is None
        or spend.answer.data.units.kind is not UnitKind.CURRENCY
        or any(
            item.answer.data.units is None or item.answer.data.units.kind is not UnitKind.RATIO
            for item in models
        )
    ):
        reasons.add(ChannelInsufficiencyReason.INCOMPATIBLE_SCOPE)
    currency = None if spend.answer.data.units is None else spend.answer.data.units.currency_code
    spend_rows = _rows(spend.answer)
    model_rows = tuple(_rows(item.answer) for item in models)
    key_sets = (set(spend_rows), *(set(values) for values in model_rows))
    if not key_sets[0] or any(keys != key_sets[0] for keys in key_sets[1:]):
        reasons.add(ChannelInsufficiencyReason.INCOMPATIBLE_SCOPE)
    marketplaces = {marketplace for marketplace, _channel in key_sets[0]}
    if len(marketplaces) != 1:
        reasons.add(ChannelInsufficiencyReason.MULTIPLE_MARKETPLACES)
    if any(value is None for value in spend_rows.values()) or any(
        value is None for values in model_rows for value in values.values()
    ):
        reasons.add(ChannelInsufficiencyReason.MISSING_VALUE)
    return _AlignedEvidence(
        reasons=frozenset(reasons),
        currency=currency,
        marketplace=next(iter(marketplaces)) if len(marketplaces) == 1 else None,
        spend_rows=spend_rows,
        model_rows=model_rows,
        key_set=frozenset(key_sets[0]),
    )


def _model_ranks(
    evidence: _AlignedEvidence,
) -> tuple[tuple[dict[str, int], ...], tuple[ModelLeaders, ...]]:
    if evidence.reasons:
        return (
            tuple({} for _model in MODEL_IDS),
            tuple(ModelLeaders(model, ()) for model in MODEL_IDS),
        )
    ranks: list[dict[str, int]] = []
    leaders: list[ModelLeaders] = []
    for model_id, model_values in zip(MODEL_IDS, evidence.model_rows, strict=True):
        channel_values = {
            channel: cast("Decimal", value)
            for (marketplace, channel), value in model_values.items()
            if marketplace == evidence.marketplace
        }
        model_ranks, model_leaders = _ranked(model_id, channel_values)
        ranks.append(model_ranks)
        leaders.append(model_leaders)
    return tuple(ranks), tuple(leaders)


def _outcome(
    reasons: frozenset[ChannelInsufficiencyReason], leaders: tuple[ModelLeaders, ...]
) -> tuple[ChannelRobustnessOutcome, str | None]:
    if reasons:
        return ChannelRobustnessOutcome.INSUFFICIENT, None
    unique = all(len(item.channel_keys) == 1 for item in leaders)
    leader_keys = {item.channel_keys[0] for item in leaders if item.channel_keys}
    if unique and len(leader_keys) == 1:
        return ChannelRobustnessOutcome.ROBUST_LEADER, next(iter(leader_keys))
    return ChannelRobustnessOutcome.MODEL_SENSITIVE, None


def channel_attribution_robustness(
    request: ChannelRobustnessRequest,
) -> ChannelAttributionRobustness:
    """Join exact models by canonical channel or close stable publication absence."""
    page = request.page
    if page is None:
        return ChannelAttributionRobustness(
            outcome=ChannelRobustnessOutcome.INSUFFICIENT,
            period=request.period,
            marketplace_key=None,
            currency=None,
            rows=(),
            leaders_by_model=tuple(ModelLeaders(model, ()) for model in MODEL_IDS),
            agreed_leader_key=None,
            insufficiency_reasons=(ChannelInsufficiencyReason.MISSING_PUBLICATION,),
            boundaries=BOUNDARIES,
            qualifiers=(),
        )

    measurements = page.measurements
    expected = (
        (SPEND_METRIC, None),
        *((ROAS_METRIC, model) for model in MODEL_IDS),
    )
    actual = tuple((item.metric_name, item.variant_id) for item in measurements)
    if actual != expected:
        raise ValueError("channel page measurements do not match the registered model set.")

    evidence = _aligned_evidence(measurements, request.period)
    ranks_by_model, leaders = _model_ranks(evidence)
    model_measurements = measurements[1:]
    artifact_rows = tuple(
        ChannelRobustnessRow(
            channel_key=channel,
            spend=evidence.spend_rows[(marketplace, channel)],
            model_results=tuple(
                ChannelModelResult(
                    model_id=model,
                    value=evidence.model_rows[index].get((marketplace, channel)),
                    rank=ranks_by_model[index].get(channel),
                    evidence=model_measurements[index].availability,
                )
                for index, model in enumerate(MODEL_IDS)
            ),
        )
        for marketplace, channel in sorted(evidence.key_set)
        if evidence.marketplace is None or marketplace == evidence.marketplace
    )
    outcome, agreed = _outcome(evidence.reasons, leaders)
    return ChannelAttributionRobustness(
        outcome=outcome,
        period=request.period,
        marketplace_key=evidence.marketplace,
        currency=evidence.currency,
        rows=artifact_rows,
        leaders_by_model=leaders,
        agreed_leader_key=agreed,
        insufficiency_reasons=tuple(sorted(evidence.reasons, key=str)),
        boundaries=BOUNDARIES,
        qualifiers=_qualifiers(measurements),
    )


def channel_robustness_payload(
    artifact: ChannelAttributionRobustness,
    labels: tuple[PresentationDimensionLabel, ...],
) -> dict[str, object]:
    """Project canonical identities to governed display labels for browser and model use."""
    index = {(label.selector_field, label.selector_value): label.display_value for label in labels}

    def display(field: str, value: str | None) -> str | None:
        if value is None:
            return None
        found = index.get((field, value))
        if found is None:
            raise ValueError("channel robustness artifact lacks a governed public label.")
        return found

    return {
        "agreed_leader": display("channel_key", artifact.agreed_leader_key),
        "boundaries": list(artifact.boundaries),
        "currency": artifact.currency,
        "insufficiency_reasons": [reason.value for reason in artifact.insufficiency_reasons],
        "leaders_by_model": {
            leader.model_id: [display("channel_key", key) for key in leader.channel_keys]
            for leader in artifact.leaders_by_model
        },
        "marketplace": display("marketplace_key", artifact.marketplace_key),
        "models": list(MODEL_IDS),
        "outcome": artifact.outcome.value,
        "period": {
            "end": artifact.period.end.isoformat(),
            "start": artifact.period.start.isoformat(),
        },
        "qualifiers": list(artifact.qualifiers),
        "rows": [
            {
                "channel": display("channel_key", row.channel_key),
                "model_results": {
                    result.model_id: {
                        "evidence": result.evidence.value,
                        "rank": result.rank,
                        "value": None if result.value is None else str(result.value),
                    }
                    for result in row.model_results
                },
                "spend": None if row.spend is None else str(row.spend),
            }
            for row in artifact.rows
        ],
    }


# The deployed v1 artifact is exactly one period.  Keep its class identity and
# constructor unchanged while giving v2 an explicit reusable period vocabulary.
ChannelPeriodRobustness = ChannelAttributionRobustness


class ChannelPeriodRelation(StrEnum):
    """Closed current-versus-comparison ROAS-ranking relations."""

    LEADER_PERSISTED = "leader_persisted"
    LEADER_CHANGED = "leader_changed"
    SENSITIVITY_PERSISTED = "sensitivity_persisted"
    SENSITIVITY_CHANGED = "sensitivity_changed"
    NOT_COMPARABLE = "not_comparable"


class ChannelDailyMovementOutcome(StrEnum):
    """Closed evidence states for one selected channel's daily supplement."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ChannelDailyReasonCode(StrEnum):
    """Closed daily insufficiency and gap reason vocabulary."""

    UNSUPPORTED_PERIOD_SIZE = "unsupported_period_size"
    ARTIFACT_OVERFLOW = "artifact_overflow"
    MISSING_PUBLICATION = "missing_publication"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    TRUNCATED = "truncated"
    QUERY_REFUSED = "query_refused"
    TOO_FEW_DATES = "too_few_dates"
    NO_USABLE_SPEND_SERIES = "no_usable_spend_series"
    NO_USABLE_MODEL_SERIES = "no_usable_model_series"
    MISSING_DATE = "missing_date"
    MISSING_SPEND = "missing_spend"
    SUPPRESSED_SPEND = "suppressed_spend"
    SUPPRESSED_MODEL = "suppressed_model"
    UNDEFINED_RATIO = "undefined_ratio"


class ChannelDailyGapField(StrEnum):
    """Which model-independent or model-specific daily field has a gap."""

    SPEND = "spend"
    MODEL = "model"


_DAILY_HARD_REASON_ORDER = (
    ChannelDailyReasonCode.UNSUPPORTED_PERIOD_SIZE,
    ChannelDailyReasonCode.ARTIFACT_OVERFLOW,
    ChannelDailyReasonCode.MISSING_PUBLICATION,
    ChannelDailyReasonCode.INCOMPATIBLE_SCOPE,
    ChannelDailyReasonCode.TRUNCATED,
    ChannelDailyReasonCode.QUERY_REFUSED,
    ChannelDailyReasonCode.TOO_FEW_DATES,
    ChannelDailyReasonCode.NO_USABLE_SPEND_SERIES,
    ChannelDailyReasonCode.NO_USABLE_MODEL_SERIES,
)
_DAILY_PARTIAL_REASON_ORDER = (
    ChannelDailyReasonCode.MISSING_DATE,
    ChannelDailyReasonCode.MISSING_SPEND,
    ChannelDailyReasonCode.SUPPRESSED_SPEND,
    ChannelDailyReasonCode.SUPPRESSED_MODEL,
    ChannelDailyReasonCode.UNDEFINED_RATIO,
)
_DAILY_REASON_ORDER = _DAILY_HARD_REASON_ORDER + _DAILY_PARTIAL_REASON_ORDER
_DAILY_REASON_POSITION = {reason: position for position, reason in enumerate(_DAILY_REASON_ORDER)}
_DAILY_EXTERNAL_HARD_REASONS = frozenset(_DAILY_HARD_REASON_ORDER[:6])
_DAILY_HARD_REASONS = frozenset(_DAILY_HARD_REASON_ORDER)
_DAILY_PARTIAL_REASONS = frozenset(_DAILY_PARTIAL_REASON_ORDER)


@dataclass(frozen=True, slots=True)
class ChannelDailyReason:
    """One stable daily reason, optionally bound to one registered model."""

    code: ChannelDailyReasonCode
    model_id: str | None = None

    def __post_init__(self) -> None:
        model_specific = self.code is ChannelDailyReasonCode.NO_USABLE_MODEL_SERIES
        if model_specific != (self.model_id is not None):
            raise ValueError("only no_usable_model_series requires one exact channel model id.")
        if self.model_id is not None and self.model_id not in MODEL_IDS:
            raise ValueError("daily reason model id is not registered.")

    @property
    def value(self) -> str:
        """Render the canonical reason code, including model custody when required."""
        if self.model_id is None:
            return self.code.value
        return f"{self.code.value}:{self.model_id}"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ChannelDailyMetricValue:
    """One exact daily value and its source evidence state."""

    value: Decimal | None
    evidence: AvailabilityState

    def __post_init__(self) -> None:
        _validate_daily_value(self.value, self.evidence)


@dataclass(frozen=True, slots=True)
class ChannelDailyModelResult:
    """One registered model's exact source-date ratio and evidence state."""

    model_id: str
    value: Decimal | None
    evidence: AvailabilityState

    def __post_init__(self) -> None:
        if self.model_id not in MODEL_IDS:
            raise ValueError("daily channel model id is not registered.")
        _validate_daily_value(self.value, self.evidence)


@dataclass(frozen=True, slots=True)
class ChannelDailyDay:
    """One source date with model-independent spend and every model ratio."""

    source_date: date
    spend: ChannelDailyMetricValue
    model_results: tuple[ChannelDailyModelResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.source_date), date):
            raise TypeError("channel daily row requires one source date.")
        if tuple(result.model_id for result in self.model_results) != MODEL_IDS:
            raise ValueError("channel daily row must preserve the registered model order.")


@dataclass(frozen=True, slots=True)
class ChannelDailyGap:
    """Exact date, field, model custody, and reason for one absent daily value."""

    source_date: date
    field: ChannelDailyGapField
    reason: ChannelDailyReasonCode
    model_id: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in _DAILY_PARTIAL_REASONS:
            raise ValueError("daily gaps require one partial evidence reason.")
        model_gap = self.field is ChannelDailyGapField.MODEL
        if model_gap != (self.model_id is not None):
            raise ValueError("only a model gap requires one exact model id.")
        if self.model_id is not None and self.model_id not in MODEL_IDS:
            raise ValueError("daily gap model id is not registered.")
        if self.field is ChannelDailyGapField.SPEND and self.reason not in {
            ChannelDailyReasonCode.MISSING_DATE,
            ChannelDailyReasonCode.MISSING_SPEND,
            ChannelDailyReasonCode.SUPPRESSED_SPEND,
        }:
            raise ValueError("spend gap reason does not describe spend evidence.")
        if self.field is ChannelDailyGapField.MODEL and self.reason not in {
            ChannelDailyReasonCode.MISSING_DATE,
            ChannelDailyReasonCode.SUPPRESSED_MODEL,
            ChannelDailyReasonCode.UNDEFINED_RATIO,
        }:
            raise ValueError("model gap reason does not describe model evidence.")


@dataclass(frozen=True, slots=True)
class ChannelDailyMovementRequest:
    """Typed source-date evidence and preflight outcomes for the pure daily reducer."""

    channel_key: str
    period: DateRange
    marketplace_key: str | None
    currency: str | None
    days: tuple[ChannelDailyDay, ...]
    hard_reasons: tuple[ChannelDailyReasonCode, ...] = ()
    qualifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.channel_key.strip():
            raise ValueError("channel daily movement requires a canonical channel key.")
        if self.marketplace_key is not None and not self.marketplace_key.strip():
            raise ValueError("channel daily marketplace key cannot be blank.")
        if self.currency is not None and not self.currency.strip():
            raise ValueError("channel daily currency cannot be blank.")
        if len(set(self.hard_reasons)) != len(self.hard_reasons):
            raise ValueError("channel daily preflight reasons must be unique.")
        if any(reason not in _DAILY_EXTERNAL_HARD_REASONS for reason in self.hard_reasons):
            raise ValueError("channel daily preflight carries a derived or partial reason.")
        if any(not qualifier.strip() for qualifier in self.qualifiers):
            raise ValueError("channel daily qualifiers cannot be blank.")


@dataclass(frozen=True, slots=True)
class ChannelDailyMovement:
    """Deterministic selected-channel daily evidence with exact gap custody."""

    outcome: ChannelDailyMovementOutcome
    channel_key: str
    period: DateRange
    marketplace_key: str | None
    currency: str | None
    days: tuple[ChannelDailyDay, ...]
    gaps: tuple[ChannelDailyGap, ...]
    insufficiency_reasons: tuple[ChannelDailyReason, ...]
    qualifiers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.channel_key.strip():
            raise ValueError("channel daily movement requires a canonical channel key.")
        expected_dates = _expected_daily_dates(self.period)
        if tuple(day.source_date for day in self.days) != expected_dates:
            raise ValueError("channel daily rows must cover the period in source-date order.")
        if self.gaps != tuple(sorted(self.gaps, key=_daily_gap_sort_key)):
            raise ValueError("channel daily gaps must preserve canonical date and field order.")
        if len(set(self.gaps)) != len(self.gaps):
            raise ValueError("channel daily movement cannot repeat a gap.")
        if any(gap.source_date not in set(expected_dates) for gap in self.gaps):
            raise ValueError("channel daily gap falls outside the selected period.")
        if self.insufficiency_reasons != tuple(
            sorted(self.insufficiency_reasons, key=_daily_reason_sort_key)
        ):
            raise ValueError("channel daily reasons must preserve documented precedence.")
        if len(set(self.insufficiency_reasons)) != len(self.insufficiency_reasons):
            raise ValueError("channel daily movement cannot repeat a reason.")
        if self.qualifiers != tuple(sorted(set(self.qualifiers))):
            raise ValueError("channel daily qualifiers must be sorted and unique.")

        has_hard_reason = any(
            reason.code in _DAILY_HARD_REASONS for reason in self.insufficiency_reasons
        )
        if (self.outcome is ChannelDailyMovementOutcome.INSUFFICIENT) != has_hard_reason:
            raise ValueError("only hard daily reasons produce insufficient movement.")
        if self.outcome is ChannelDailyMovementOutcome.COMPLETE:
            complete_cells = all(
                _usable_daily_value(day.spend.value, day.spend.evidence)
                and all(
                    _usable_daily_value(result.value, result.evidence)
                    for result in day.model_results
                )
                for day in self.days
            )
            if not complete_cells or self.gaps or self.insufficiency_reasons or self.qualifiers:
                raise ValueError("complete daily movement requires every unqualified value.")
        elif self.outcome is ChannelDailyMovementOutcome.PARTIAL and not (
            self.gaps or self.qualifiers
        ):
            raise ValueError("partial daily movement requires a gap or qualifier.")


def _validate_daily_value(value: Decimal | None, evidence: AvailabilityState) -> None:
    if not isinstance(cast("object", evidence), AvailabilityState):
        raise TypeError("daily channel evidence state is invalid.")
    if value is not None and (
        not isinstance(cast("object", value), Decimal) or not value.is_finite()
    ):
        raise ValueError("daily channel values must be finite Decimals when present.")
    if value is not None and evidence in {
        AvailabilityState.EMPTY,
        AvailabilityState.SUPPRESSED,
    }:
        raise ValueError("empty or suppressed daily evidence cannot carry a value.")


def _expected_daily_dates(period: DateRange) -> tuple[date, ...]:
    return tuple(period.start + timedelta(days=offset) for offset in range(period.days))


def _daily_reason_sort_key(reason: ChannelDailyReason) -> tuple[int, int]:
    model_position = -1 if reason.model_id is None else MODEL_IDS.index(reason.model_id)
    return _DAILY_REASON_POSITION[reason.code], model_position


def _daily_gap_sort_key(gap: ChannelDailyGap) -> tuple[date, int, int, int]:
    field_position = 0 if gap.field is ChannelDailyGapField.SPEND else 1
    model_position = -1 if gap.model_id is None else MODEL_IDS.index(gap.model_id)
    return (
        gap.source_date,
        field_position,
        model_position,
        _DAILY_REASON_POSITION[gap.reason],
    )


def _usable_daily_value(value: Decimal | None, evidence: AvailabilityState) -> bool:
    return value is not None and evidence is AvailabilityState.AVAILABLE


def _missing_daily_day(source_date: date) -> ChannelDailyDay:
    return ChannelDailyDay(
        source_date=source_date,
        spend=ChannelDailyMetricValue(None, AvailabilityState.EMPTY),
        model_results=tuple(
            ChannelDailyModelResult(model_id, None, AvailabilityState.EMPTY)
            for model_id in MODEL_IDS
        ),
    )


def _daily_gaps(day: ChannelDailyDay, *, missing_date: bool) -> tuple[ChannelDailyGap, ...]:
    if missing_date:
        return (
            ChannelDailyGap(
                day.source_date,
                ChannelDailyGapField.SPEND,
                ChannelDailyReasonCode.MISSING_DATE,
            ),
            *(
                ChannelDailyGap(
                    day.source_date,
                    ChannelDailyGapField.MODEL,
                    ChannelDailyReasonCode.MISSING_DATE,
                    result.model_id,
                )
                for result in day.model_results
            ),
        )

    gaps: list[ChannelDailyGap] = []
    if day.spend.evidence is AvailabilityState.SUPPRESSED:
        gaps.append(
            ChannelDailyGap(
                day.source_date,
                ChannelDailyGapField.SPEND,
                ChannelDailyReasonCode.SUPPRESSED_SPEND,
            )
        )
    elif day.spend.value is None:
        gaps.append(
            ChannelDailyGap(
                day.source_date,
                ChannelDailyGapField.SPEND,
                ChannelDailyReasonCode.MISSING_SPEND,
            )
        )
    for result in day.model_results:
        if result.evidence is AvailabilityState.SUPPRESSED:
            gaps.append(
                ChannelDailyGap(
                    day.source_date,
                    ChannelDailyGapField.MODEL,
                    ChannelDailyReasonCode.SUPPRESSED_MODEL,
                    result.model_id,
                )
            )
        elif result.value is None:
            gaps.append(
                ChannelDailyGap(
                    day.source_date,
                    ChannelDailyGapField.MODEL,
                    ChannelDailyReasonCode.UNDEFINED_RATIO,
                    result.model_id,
                )
            )
    return tuple(gaps)


_MIN_DAILY_SOURCE_DATES = 2
_MIN_USABLE_MODEL_VALUES = 2


@dataclass(frozen=True, slots=True)
class _DailySourceRows:
    rows: dict[date, ChannelDailyDay]
    incompatible_scope: bool


@dataclass(frozen=True, slots=True)
class _DailyEvidence:
    days: tuple[ChannelDailyDay, ...]
    gaps: tuple[ChannelDailyGap, ...]
    usable_spend: int
    usable_models: dict[str, int]


def _index_daily_source_rows(request: ChannelDailyMovementRequest) -> _DailySourceRows:
    expected_set = set(_expected_daily_dates(request.period))
    grouped: dict[date, list[ChannelDailyDay]] = {}
    incompatible_scope = False
    for day in request.days:
        if day.source_date not in expected_set:
            incompatible_scope = True
            continue
        grouped.setdefault(day.source_date, []).append(day)
    incompatible_scope = incompatible_scope or any(len(values) != 1 for values in grouped.values())
    return _DailySourceRows(
        rows={
            source_date: values[0] for source_date, values in grouped.items() if len(values) == 1
        },
        incompatible_scope=incompatible_scope,
    )


def _daily_hard_codes(
    request: ChannelDailyMovementRequest,
    source_rows: _DailySourceRows,
) -> set[ChannelDailyReasonCode]:
    hard_codes = set(request.hard_reasons)
    if source_rows.incompatible_scope:
        hard_codes.add(ChannelDailyReasonCode.INCOMPATIBLE_SCOPE)
    if any(
        day.spend.evidence is AvailabilityState.PARTIAL
        or any(result.evidence is AvailabilityState.PARTIAL for result in day.model_results)
        for day in request.days
    ):
        hard_codes.add(ChannelDailyReasonCode.TRUNCATED)
    if request.period.days < _MIN_DAILY_SOURCE_DATES:
        hard_codes.add(ChannelDailyReasonCode.TOO_FEW_DATES)
    return hard_codes


def _normalize_daily_evidence(
    request: ChannelDailyMovementRequest,
    source_rows: _DailySourceRows,
) -> _DailyEvidence:
    days: list[ChannelDailyDay] = []
    gaps: list[ChannelDailyGap] = []
    for source_date in _expected_daily_dates(request.period):
        source_day = source_rows.rows.get(source_date)
        missing_date = source_day is None
        day = _missing_daily_day(source_date) if source_day is None else source_day
        days.append(day)
        gaps.extend(_daily_gaps(day, missing_date=missing_date))

    usable_models: dict[str, int] = dict.fromkeys(MODEL_IDS, 0)
    for day in days:
        for result in day.model_results:
            if _usable_daily_value(result.value, result.evidence):
                usable_models[result.model_id] += 1
    return _DailyEvidence(
        days=tuple(days),
        gaps=tuple(sorted(gaps, key=_daily_gap_sort_key)),
        usable_spend=sum(_usable_daily_value(day.spend.value, day.spend.evidence) for day in days),
        usable_models=usable_models,
    )


def reduce_channel_daily_movement(
    request: ChannelDailyMovementRequest,
) -> ChannelDailyMovement:
    """Reduce exact daily evidence without interpolation, imputation, or ratio averaging."""
    source_rows = _index_daily_source_rows(request)
    hard_codes = _daily_hard_codes(request, source_rows)
    evidence = _normalize_daily_evidence(request, source_rows)
    if evidence.usable_spend == 0:
        hard_codes.add(ChannelDailyReasonCode.NO_USABLE_SPEND_SERIES)

    reasons = {
        ChannelDailyReason(code)
        for code in hard_codes
        if code is not ChannelDailyReasonCode.NO_USABLE_MODEL_SERIES
    }
    reasons.update(
        ChannelDailyReason(ChannelDailyReasonCode.NO_USABLE_MODEL_SERIES, model_id)
        for model_id in MODEL_IDS
        if evidence.usable_models[model_id] < _MIN_USABLE_MODEL_VALUES
    )
    reasons.update(ChannelDailyReason(gap.reason) for gap in evidence.gaps)
    ordered_reasons = tuple(sorted(reasons, key=_daily_reason_sort_key))
    qualifiers = tuple(sorted(set(request.qualifiers)))
    if any(reason.code in _DAILY_HARD_REASONS for reason in ordered_reasons):
        outcome = ChannelDailyMovementOutcome.INSUFFICIENT
    elif evidence.gaps or qualifiers:
        outcome = ChannelDailyMovementOutcome.PARTIAL
    else:
        outcome = ChannelDailyMovementOutcome.COMPLETE
    return ChannelDailyMovement(
        outcome=outcome,
        channel_key=request.channel_key,
        period=request.period,
        marketplace_key=request.marketplace_key,
        currency=request.currency,
        days=evidence.days,
        gaps=evidence.gaps,
        insufficiency_reasons=ordered_reasons,
        qualifiers=qualifiers,
    )


def channel_period_relation(
    current: ChannelPeriodRobustness,
    comparison: ChannelPeriodRobustness,
) -> ChannelPeriodRelation:
    """Derive stability only from the two closed period outcomes and agreed leaders."""
    if (
        current.outcome is ChannelRobustnessOutcome.INSUFFICIENT
        or comparison.outcome is ChannelRobustnessOutcome.INSUFFICIENT
    ):
        return ChannelPeriodRelation.NOT_COMPARABLE
    if (
        current.outcome is ChannelRobustnessOutcome.MODEL_SENSITIVE
        and comparison.outcome is ChannelRobustnessOutcome.MODEL_SENSITIVE
    ):
        return ChannelPeriodRelation.SENSITIVITY_PERSISTED
    if current.outcome is not comparison.outcome:
        return ChannelPeriodRelation.SENSITIVITY_CHANGED
    if current.agreed_leader_key == comparison.agreed_leader_key:
        return ChannelPeriodRelation.LEADER_PERSISTED
    return ChannelPeriodRelation.LEADER_CHANGED


@dataclass(frozen=True, slots=True)
class ChannelAttributionRobustnessV2:
    """Two exact period results, their closed relation, and optional daily evidence."""

    current: ChannelPeriodRobustness
    comparison: ChannelPeriodRobustness
    period_relation: ChannelPeriodRelation
    selected_channel_daily: ChannelDailyMovement | None = None
    boundaries: tuple[str, ...] = BOUNDARIES

    def __post_init__(self) -> None:
        if self.boundaries != BOUNDARIES:
            raise ValueError("channel v2 boundaries are fixed domain policy.")
        if (
            self.current.period.days != self.comparison.period.days
            or self.comparison.period.end != self.current.period.start - timedelta(days=1)
        ):
            raise ValueError(
                "channel v2 comparison must immediately precede the equal-length current period."
            )
        if self.period_relation is not channel_period_relation(self.current, self.comparison):
            raise ValueError("channel v2 period relation must be server-derived.")
        daily = self.selected_channel_daily
        if daily is None:
            return
        if daily.period != self.current.period:
            raise ValueError("selected daily evidence must use the exact current period.")
        if daily.channel_key not in {row.channel_key for row in self.current.rows}:
            raise ValueError("selected daily evidence must name one current-period channel.")
        if daily.marketplace_key != self.current.marketplace_key:
            raise ValueError("selected daily marketplace must match the current period.")
        if daily.currency != self.current.currency:
            raise ValueError("selected daily currency must match the current period.")


def channel_attribution_robustness_v2(
    current: ChannelPeriodRobustness,
    comparison: ChannelPeriodRobustness,
    selected_channel_daily: ChannelDailyMovement | None = None,
) -> ChannelAttributionRobustnessV2:
    """Compose v2 without deriving either weekly period result from daily ratios."""
    return ChannelAttributionRobustnessV2(
        current=current,
        comparison=comparison,
        period_relation=channel_period_relation(current, comparison),
        selected_channel_daily=selected_channel_daily,
    )


def _safe_channel_label_index(
    labels: tuple[PresentationDimensionLabel, ...],
) -> dict[tuple[str, str], PresentationDimensionLabel]:
    index = {(label.selector_field, label.selector_value): label for label in labels}
    if len(index) != len(labels):
        raise ValueError("channel robustness labels repeat an internal selector value.")
    expected_fields = {"channel_key": "channel", "marketplace_key": "marketplace"}
    if any(label.display_field != expected_fields[label.selector_field] for label in labels):
        raise ValueError("channel robustness label uses the wrong public dimension.")
    return index


def _safe_channel_display(
    index: dict[tuple[str, str], PresentationDimensionLabel],
    field: str,
    value: str | None,
) -> str | None:
    if value is None:
        return None
    label = index.get((field, value))
    if label is None:
        raise ValueError("channel robustness artifact lacks a governed public label.")
    return label.display_value


def channel_period_robustness_payload(
    artifact: ChannelPeriodRobustness,
    labels: tuple[PresentationDimensionLabel, ...],
) -> dict[str, object]:
    """Project one reusable period with strict canonical-to-public label validation."""
    _safe_channel_label_index(labels)
    return channel_robustness_payload(artifact, labels)


def channel_daily_movement_payload(
    movement: ChannelDailyMovement,
    labels: tuple[PresentationDimensionLabel, ...],
) -> dict[str, object]:
    """Project selected daily evidence without releasing canonical dimension identities."""
    index = _safe_channel_label_index(labels)
    return {
        "channel": _safe_channel_display(index, "channel_key", movement.channel_key),
        "currency": movement.currency,
        "days": [
            {
                "model_results": {
                    result.model_id: {
                        "evidence": result.evidence.value,
                        "value": None if result.value is None else str(result.value),
                    }
                    for result in day.model_results
                },
                "source_date": day.source_date.isoformat(),
                "spend": {
                    "evidence": day.spend.evidence.value,
                    "value": None if day.spend.value is None else str(day.spend.value),
                },
            }
            for day in movement.days
        ],
        "gaps": [
            {
                "field": gap.field.value,
                "model_id": gap.model_id,
                "reason": gap.reason.value,
                "source_date": gap.source_date.isoformat(),
            }
            for gap in movement.gaps
        ],
        "insufficiency_reasons": [reason.value for reason in movement.insufficiency_reasons],
        "marketplace": _safe_channel_display(index, "marketplace_key", movement.marketplace_key),
        "models": list(MODEL_IDS),
        "outcome": movement.outcome.value,
        "period": {
            "end": movement.period.end.isoformat(),
            "start": movement.period.start.isoformat(),
        },
        "qualifiers": list(movement.qualifiers),
    }


def channel_attribution_robustness_v2_payload(
    artifact: ChannelAttributionRobustnessV2,
    labels: tuple[PresentationDimensionLabel, ...],
) -> dict[str, object]:
    """Project the complete v2 artifact with boundaries owned once at the outer level."""
    current = channel_period_robustness_payload(artifact.current, labels)
    comparison = channel_period_robustness_payload(artifact.comparison, labels)
    return {
        "boundaries": list(artifact.boundaries),
        "comparison": {key: value for key, value in comparison.items() if key != "boundaries"},
        "current": {key: value for key, value in current.items() if key != "boundaries"},
        "period_relation": artifact.period_relation.value,
        "selected_channel_daily": (
            None
            if artifact.selected_channel_daily is None
            else channel_daily_movement_payload(artifact.selected_channel_daily, labels)
        ),
    }


@dataclass(frozen=True, slots=True)
class ChannelV2ParentRow:
    """One governed public channel label from a stored v2 parent artifact."""

    public_label: str


@dataclass(frozen=True, slots=True)
class ChannelV2ParentPayload:
    """Typed durable fields required to redeem one channel-row capability."""

    current_period: DateRange
    comparison_period: DateRange
    models: tuple[str, ...]
    marketplace: str
    currency: str
    current_rows: tuple[ChannelV2ParentRow, ...]


_PARENT_FIELDS = frozenset(
    {"boundaries", "comparison", "current", "period_relation", "selected_channel_daily"}
)
_PARENT_PERIOD_FIELDS = frozenset(
    {
        "agreed_leader",
        "currency",
        "insufficiency_reasons",
        "leaders_by_model",
        "marketplace",
        "models",
        "outcome",
        "period",
        "qualifiers",
        "rows",
    }
)
_PARENT_PERIOD_RANGE_FIELDS = frozenset({"end", "start"})
_PARENT_ROW_FIELDS = frozenset({"channel", "model_results", "spend"})
_PARENT_MODEL_RESULT_FIELDS = frozenset({"evidence", "rank", "value"})


def _parent_object(value: object, label: str, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} fields are not exact.")
    item = cast("dict[str, object]", value)
    if frozenset(item) != fields:
        raise ValueError(f"{label} fields are not exact.")
    return item


def _parent_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _parent_optional_text(value: object, label: str) -> None:
    if value is not None:
        _parent_text(value, label)


def _parent_text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    values = cast("list[object]", value)
    return tuple(_parent_text(item, label) for item in values)


def _parent_period(value: object, label: str) -> DateRange:
    item = _parent_object(value, label, _PARENT_PERIOD_RANGE_FIELDS)
    try:
        return DateRange(
            date.fromisoformat(_parent_text(item["start"], f"{label} start")),
            date.fromisoformat(_parent_text(item["end"], f"{label} end")),
        )
    except ValueError:
        raise ValueError(f"{label} is invalid.") from None


def _decode_parent_rows(
    value: object,
    models: tuple[str, ...],
    label: str,
) -> tuple[ChannelV2ParentRow, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} rows must be a list.")
    rows: list[ChannelV2ParentRow] = []
    for raw_row in cast("list[object]", value):
        row = _parent_object(raw_row, f"{label} row", _PARENT_ROW_FIELDS)
        public_label = _parent_text(row["channel"], f"{label} channel")
        _parent_optional_text(row["spend"], f"{label} spend")
        results = _parent_object(row["model_results"], f"{label} model results", frozenset(models))
        for result in results.values():
            item = _parent_object(result, f"{label} model result", _PARENT_MODEL_RESULT_FIELDS)
            _parent_text(item["evidence"], f"{label} model evidence")
            if item["rank"] is not None and (
                isinstance(item["rank"], bool) or not isinstance(item["rank"], int)
            ):
                raise ValueError(f"{label} model rank is invalid.")
            _parent_optional_text(item["value"], f"{label} model value")
        rows.append(ChannelV2ParentRow(public_label))
    if len({row.public_label for row in rows}) != len(rows):
        raise ValueError(f"{label} public channel labels must be unique and nonempty.")
    return tuple(rows)


def _decode_parent_period(
    value: object,
    label: str,
) -> tuple[DateRange, tuple[str, ...], str, str, tuple[ChannelV2ParentRow, ...]]:
    item = _parent_object(value, label, _PARENT_PERIOD_FIELDS)
    try:
        ChannelRobustnessOutcome(_parent_text(item["outcome"], f"{label} outcome"))
    except ValueError:
        raise ValueError(f"{label} outcome is unsupported.") from None
    models = _parent_text_list(item["models"], f"{label} models")
    if not models or len(set(models)) != len(models):
        raise ValueError(f"{label} models must be unique and nonempty.")
    leaders = item["leaders_by_model"]
    if not isinstance(leaders, dict):
        raise ValueError(f"{label} leaders must exactly cover models.")
    leader_mapping = cast("dict[str, object]", leaders)
    if frozenset(leader_mapping) != frozenset(models):
        raise ValueError(f"{label} leaders must exactly cover models.")
    for model_id, values in leader_mapping.items():
        _parent_text(model_id, f"{label} leader model")
        _parent_text_list(values, f"{label} leaders")
    _parent_optional_text(item["agreed_leader"], f"{label} agreed leader")
    _parent_text_list(item["insufficiency_reasons"], f"{label} insufficiency reasons")
    _parent_text_list(item["qualifiers"], f"{label} qualifiers")
    marketplace = _parent_text(item["marketplace"], f"{label} marketplace")
    currency = _parent_text(item["currency"], f"{label} currency")
    return (
        _parent_period(item["period"], f"{label} period"),
        models,
        marketplace,
        currency,
        _decode_parent_rows(item["rows"], models, label),
    )


def decode_channel_v2_parent(value: object) -> ChannelV2ParentPayload:
    """Decode only a complete parent v2 payload suitable for row-capability redemption."""
    item = _parent_object(value, "channel v2 parent", _PARENT_FIELDS)
    _parent_text_list(item["boundaries"], "channel v2 boundaries")
    if item["selected_channel_daily"] is not None:
        raise ValueError("channel v2 parent must not include selected daily evidence.")
    try:
        ChannelPeriodRelation(_parent_text(item["period_relation"], "channel v2 period relation"))
    except ValueError:
        raise ValueError("channel v2 period relation is unsupported.") from None
    current_period, models, marketplace, currency, current_rows = _decode_parent_period(
        item["current"], "channel v2 current"
    )
    comparison_period, comparison_models, comparison_marketplace, comparison_currency, _ = (
        _decode_parent_period(item["comparison"], "channel v2 comparison")
    )
    if (
        models != comparison_models
        or marketplace != comparison_marketplace
        or currency != comparison_currency
        or current_period.days != comparison_period.days
        or comparison_period.end != current_period.start - timedelta(days=1)
    ):
        raise ValueError("channel v2 parent periods are incompatible.")
    return ChannelV2ParentPayload(
        current_period=current_period,
        comparison_period=comparison_period,
        models=models,
        marketplace=marketplace,
        currency=currency,
        current_rows=current_rows,
    )


# Concise compatibility aliases for callers that use the shorter approved vocabulary.
ChannelDailyOutcome = ChannelDailyMovementOutcome
ChannelDailyField = ChannelDailyGapField
ChannelRobustnessV2 = ChannelAttributionRobustnessV2
derive_channel_period_relation = channel_period_relation
channel_daily_movement = reduce_channel_daily_movement
channel_robustness_v2 = channel_attribution_robustness_v2
channel_robustness_v2_payload = channel_attribution_robustness_v2_payload
