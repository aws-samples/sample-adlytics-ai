# SPDX-License-Identifier: MIT-0
"""Channel attribution robustness v1 compatibility and pure v2 domain semantics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

from domain.narrative.shaped_response import (
    Metadata,
    ResponseData,
    ShapedResponse,
    UnitKind,
    Units,
)
from domain.presentation.analytical_pages import (
    AnalyticalPageResult,
    AvailabilityState,
    PageMeasurementResult,
    PresentationDimensionLabel,
)
from domain.presentation.channel_robustness import (
    BOUNDARIES,
    MODEL_IDS,
    ROAS_METRIC,
    SPEND_METRIC,
    ChannelAttributionRobustness,
    ChannelAttributionRobustnessV2,
    ChannelDailyDay,
    ChannelDailyGapField,
    ChannelDailyMetricValue,
    ChannelDailyModelResult,
    ChannelDailyMovementOutcome,
    ChannelDailyMovementRequest,
    ChannelDailyReasonCode,
    ChannelInsufficiencyReason,
    ChannelModelResult,
    ChannelPeriodRelation,
    ChannelPeriodRobustness,
    ChannelRobustnessOutcome,
    ChannelRobustnessRequest,
    ChannelRobustnessRow,
    ModelLeaders,
    channel_attribution_robustness,
    channel_attribution_robustness_v2,
    channel_attribution_robustness_v2_payload,
    channel_period_relation,
    channel_period_robustness_payload,
    channel_robustness_payload,
    decode_channel_v2_parent,
    reduce_channel_daily_movement,
)
from domain.presentation.page_schemas import (
    AnalyticalPageSchema,
    Cardinality,
    MetricRole,
    PageArtifactDeclaration,
    PageGrain,
    PageMetric,
    PageMetricVariant,
    PageMode,
    PageSection,
    PageTime,
)
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange, Grain

pytestmark = pytest.mark.unit

CURRENT = DateRange(date(2026, 8, 10), date(2026, 8, 12))
COMPARISON = DateRange(date(2026, 8, 7), date(2026, 8, 9))
MARKETPLACE_KEY = "marketplace:fictional"
CHANNEL_A = "channel:fictional-a"
CHANNEL_B = "channel:fictional-b"
LABELS = (
    PresentationDimensionLabel("channel_key", CHANNEL_A, "channel", "Fictional A"),
    PresentationDimensionLabel("channel_key", CHANNEL_B, "channel", "Fictional B"),
    PresentationDimensionLabel(
        "marketplace_key", MARKETPLACE_KEY, "marketplace", "Fictional marketplace"
    ),
)

SCHEMA = AnalyticalPageSchema(
    page_id="amazon-channel-performance",
    display_name="Amazon Channel Performance",
    route="channel-performance",
    purpose="Test exact channel ranking robustness.",
    primary_job="Test the complete registered model set.",
    mode=PageMode.PARALLEL,
    boundary_required=True,
    grain=PageGrain(PageTime.PERIOD, ("channel", "marketplace")),
    preferred_window_duration_days=7,
    metrics=(
        PageMetric(SPEND_METRIC, (MetricRole.TABLE,)),
        PageMetric(
            ROAS_METRIC,
            (MetricRole.TABLE,),
            tuple(
                PageMetricVariant(model_id, (("attribution_model_key", model_id),))
                for model_id in MODEL_IDS
            ),
        ),
    ),
    sections=(
        PageSection("matrix", Cardinality.EXACTLY_ONE),
        PageSection("boundary", Cardinality.EXACTLY_ONE),
    ),
    artifact=PageArtifactDeclaration(
        "ChannelAttributionRobustness",
        1,
        ("robust_leader", "model_sensitive", "insufficient"),
    ),
)


def _answer(
    values: tuple[dict[str, str | int | Decimal | None], ...],
    units: Units,
    *,
    truncated: bool = False,
) -> ShapedResponse:
    start = datetime.combine(CURRENT.start, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(CURRENT.end, datetime.min.time(), tzinfo=UTC)
    return ShapedResponse(
        data=ResponseData(
            values=values,
            units=units,
            grain=Grain(("channel", "marketplace")),
        ),
        narrative=("Results were truncated.",) if truncated else ("Exact evidence.",),
        metadata=Metadata(
            facts_read=("fct_fictional",),
            measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
            coverage_start=start,
            coverage_end=end,
            freshness_status=FreshnessStatus.FRESH,
            suppression=Suppression(False),
            row_count=len(values),
            computed_at=end,
            correlation_id="fictional-correlation",
            requested_coverage_intervals=(CURRENT,),
            actual_coverage_intervals=(CURRENT,),
            source_timezone="UTC",
            truncated=truncated,
        ),
    )


def _measurement_rows(values: dict[str, Decimal]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "channel_key": channel_key,
            "marketplace_key": MARKETPLACE_KEY,
            "value": value,
        }
        for channel_key, value in values.items()
    )


def _v1_request(*, truncated_model: str | None = None) -> ChannelRobustnessRequest:
    spend = PageMeasurementResult(
        SPEND_METRIC,
        (MetricRole.TABLE,),
        None,
        AvailabilityState.AVAILABLE,
        _answer(
            cast(
                "tuple[dict[str, str | int | Decimal | None], ...]",
                _measurement_rows({CHANNEL_B: Decimal("50.00"), CHANNEL_A: Decimal("100.00")}),
            ),
            Units(UnitKind.CURRENCY, "USD"),
        ),
    )
    values_by_model = {
        MODEL_IDS[0]: {CHANNEL_B: Decimal("2.0"), CHANNEL_A: Decimal("4.0")},
        MODEL_IDS[1]: {CHANNEL_B: Decimal("1.0"), CHANNEL_A: Decimal("3.0")},
        MODEL_IDS[2]: {CHANNEL_B: Decimal("0"), CHANNEL_A: Decimal("2.0")},
    }
    models = tuple(
        PageMeasurementResult(
            ROAS_METRIC,
            (MetricRole.TABLE,),
            model_id,
            (
                AvailabilityState.PARTIAL
                if model_id == truncated_model
                else AvailabilityState.AVAILABLE
            ),
            _answer(
                cast(
                    "tuple[dict[str, str | int | Decimal | None], ...]",
                    _measurement_rows(values_by_model[model_id]),
                ),
                Units(UnitKind.RATIO),
                truncated=model_id == truncated_model,
            ),
        )
        for model_id in MODEL_IDS
    )
    return ChannelRobustnessRequest(AnalyticalPageResult(SCHEMA, (spend, *models)), CURRENT)


def _period_row(
    channel_key: str,
    outcome: ChannelRobustnessOutcome,
    leader: str,
) -> ChannelRobustnessRow:
    results: list[ChannelModelResult] = []
    for index, model_id in enumerate(MODEL_IDS):
        if outcome is ChannelRobustnessOutcome.INSUFFICIENT:
            value = None
            rank = None
        elif outcome is ChannelRobustnessOutcome.ROBUST_LEADER:
            value = Decimal("2") if channel_key == leader else Decimal("1")
            rank = 1 if channel_key == leader else 2
        elif index == 0:
            value = Decimal("2") if channel_key == CHANNEL_A else Decimal("1")
            rank = 1 if channel_key == CHANNEL_A else 2
        elif index == 1:
            value = Decimal("2") if channel_key == CHANNEL_B else Decimal("1")
            rank = 1 if channel_key == CHANNEL_B else 2
        else:
            value = Decimal("2")
            rank = 1
        results.append(ChannelModelResult(model_id, value, rank, AvailabilityState.AVAILABLE))
    return ChannelRobustnessRow(
        channel_key,
        Decimal("100.00") if channel_key == CHANNEL_A else Decimal("50.00"),
        tuple(results),
    )


def _period(
    outcome: ChannelRobustnessOutcome,
    period: DateRange = CURRENT,
    leader: str = CHANNEL_A,
) -> ChannelPeriodRobustness:
    if outcome is ChannelRobustnessOutcome.ROBUST_LEADER:
        leaders = tuple(ModelLeaders(model_id, (leader,)) for model_id in MODEL_IDS)
        agreed = leader
        reasons: tuple[ChannelInsufficiencyReason, ...] = ()
    elif outcome is ChannelRobustnessOutcome.MODEL_SENSITIVE:
        leaders = (
            ModelLeaders(MODEL_IDS[0], (CHANNEL_A,)),
            ModelLeaders(MODEL_IDS[1], (CHANNEL_B,)),
            ModelLeaders(MODEL_IDS[2], (CHANNEL_A, CHANNEL_B)),
        )
        agreed = None
        reasons = ()
    else:
        leaders = tuple(ModelLeaders(model_id, ()) for model_id in MODEL_IDS)
        agreed = None
        reasons = (ChannelInsufficiencyReason.EMPTY,)
    return ChannelAttributionRobustness(
        outcome=outcome,
        period=period,
        marketplace_key=MARKETPLACE_KEY,
        currency="USD",
        rows=tuple(
            _period_row(channel_key, outcome, leader) for channel_key in (CHANNEL_A, CHANNEL_B)
        ),
        leaders_by_model=leaders,
        agreed_leader_key=agreed,
        insufficiency_reasons=reasons,
        boundaries=BOUNDARIES,
        qualifiers=(),
    )


def _daily_models(
    values: tuple[Decimal | None, ...] = (
        Decimal("1.0"),
        Decimal("2.0"),
        Decimal("3.0"),
    ),
    evidence: tuple[AvailabilityState, ...] = (
        AvailabilityState.AVAILABLE,
        AvailabilityState.AVAILABLE,
        AvailabilityState.AVAILABLE,
    ),
) -> tuple[ChannelDailyModelResult, ...]:
    return tuple(
        ChannelDailyModelResult(model_id, value, state)
        for model_id, value, state in zip(MODEL_IDS, values, evidence, strict=True)
    )


type _DailyModelFixture = tuple[Decimal | None, ...] | tuple[ChannelDailyModelResult, ...]


def _day(
    source_date: date,
    spend: Decimal | ChannelDailyMetricValue | None = Decimal("10.00"),
    model_values: _DailyModelFixture = (
        Decimal("1.0"),
        Decimal("2.0"),
        Decimal("3.0"),
    ),
) -> ChannelDailyDay:
    spend_result = (
        spend
        if isinstance(spend, ChannelDailyMetricValue)
        else ChannelDailyMetricValue(
            spend,
            AvailabilityState.AVAILABLE if spend is not None else AvailabilityState.EMPTY,
        )
    )
    model_results = (
        cast("tuple[ChannelDailyModelResult, ...]", model_values)
        if all(isinstance(value, ChannelDailyModelResult) for value in model_values)
        else _daily_models(cast("tuple[Decimal | None, ...]", model_values))
    )
    return ChannelDailyDay(source_date, spend_result, model_results)


def _daily_request(
    days: tuple[ChannelDailyDay, ...],
    period: DateRange = CURRENT,
    hard_reasons: tuple[ChannelDailyReasonCode, ...] = (),
) -> ChannelDailyMovementRequest:
    return ChannelDailyMovementRequest(
        channel_key=CHANNEL_A,
        period=period,
        marketplace_key=MARKETPLACE_KEY,
        currency="USD",
        days=days,
        hard_reasons=hard_reasons,
    )


def _assert_selector_safe(value: object) -> None:
    forbidden = {CHANNEL_A, CHANNEL_B, MARKETPLACE_KEY, "channel_key", "marketplace_key"}
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        assert not forbidden.intersection(key for key in mapping if isinstance(key, str))
        for nested in mapping.values():
            _assert_selector_safe(nested)
    elif isinstance(value, list):
        for nested in cast("list[object]", value):
            _assert_selector_safe(nested)
    elif isinstance(value, str):
        assert value not in forbidden


def test_v1_reducer_symbols_ranks_and_payload_remain_compatible() -> None:
    artifact = channel_attribution_robustness(_v1_request())

    assert isinstance(artifact, ChannelPeriodRobustness)
    assert artifact.outcome is ChannelRobustnessOutcome.ROBUST_LEADER
    assert artifact.agreed_leader_key == CHANNEL_A
    assert artifact.currency == "USD"
    assert artifact.period == CURRENT
    assert tuple(row.channel_key for row in artifact.rows) == (CHANNEL_A, CHANNEL_B)
    assert tuple(
        (result.model_id, result.value, result.rank) for result in artifact.rows[0].model_results
    ) == (
        (MODEL_IDS[0], Decimal("4.0"), 1),
        (MODEL_IDS[1], Decimal("3.0"), 1),
        (MODEL_IDS[2], Decimal("2.0"), 1),
    )
    assert artifact.rows[0].spend == Decimal("100.00")

    payload = channel_robustness_payload(artifact, LABELS)
    assert payload["agreed_leader"] == "Fictional A"
    assert payload["marketplace"] == "Fictional marketplace"
    assert payload["models"] == list(MODEL_IDS)
    assert cast("dict[str, object]", cast("list[object]", payload["rows"])[0])["spend"] == (
        "100.00"
    )
    _assert_selector_safe(payload)


def test_v1_projector_keeps_last_duplicate_label_while_new_projector_refuses_it() -> None:
    artifact = _period(ChannelRobustnessOutcome.ROBUST_LEADER)
    duplicate_labels = (
        *LABELS,
        PresentationDimensionLabel("channel_key", CHANNEL_A, "channel", "Replacement A"),
    )

    assert channel_robustness_payload(artifact, duplicate_labels)["agreed_leader"] == (
        "Replacement A"
    )
    with pytest.raises(ValueError, match="repeat"):
        channel_period_robustness_payload(artifact, duplicate_labels)


def test_v1_partial_availability_remains_typed_insufficiency() -> None:
    with pytest.raises(ValueError, match="value and rank"):
        channel_attribution_robustness(_v1_request(truncated_model=MODEL_IDS[1]))


@pytest.mark.parametrize(
    ("current", "comparison", "expected"),
    [
        (
            _period(ChannelRobustnessOutcome.ROBUST_LEADER, leader=CHANNEL_A),
            _period(
                ChannelRobustnessOutcome.ROBUST_LEADER,
                period=COMPARISON,
                leader=CHANNEL_A,
            ),
            ChannelPeriodRelation.LEADER_PERSISTED,
        ),
        (
            _period(ChannelRobustnessOutcome.ROBUST_LEADER, leader=CHANNEL_A),
            _period(
                ChannelRobustnessOutcome.ROBUST_LEADER,
                period=COMPARISON,
                leader=CHANNEL_B,
            ),
            ChannelPeriodRelation.LEADER_CHANGED,
        ),
        (
            _period(ChannelRobustnessOutcome.MODEL_SENSITIVE),
            _period(ChannelRobustnessOutcome.MODEL_SENSITIVE, period=COMPARISON),
            ChannelPeriodRelation.SENSITIVITY_PERSISTED,
        ),
        (
            _period(ChannelRobustnessOutcome.ROBUST_LEADER),
            _period(ChannelRobustnessOutcome.MODEL_SENSITIVE, period=COMPARISON),
            ChannelPeriodRelation.SENSITIVITY_CHANGED,
        ),
        (
            _period(ChannelRobustnessOutcome.ROBUST_LEADER),
            _period(ChannelRobustnessOutcome.INSUFFICIENT, period=COMPARISON),
            ChannelPeriodRelation.NOT_COMPARABLE,
        ),
    ],
)
def test_every_period_relation_is_independent_of_both_row_orders(
    current: ChannelPeriodRobustness,
    comparison: ChannelPeriodRobustness,
    expected: ChannelPeriodRelation,
) -> None:
    assert channel_period_relation(current, comparison) is expected
    assert (
        channel_period_relation(
            replace(current, rows=tuple(reversed(current.rows))),
            replace(comparison, rows=tuple(reversed(comparison.rows))),
        )
        is expected
    )


def test_daily_complete_orders_dates_and_preserves_zero_as_available() -> None:
    movement = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(date(2026, 8, 12)),
                _day(
                    date(2026, 8, 10),
                    spend=Decimal("0"),
                    model_values=(Decimal("0"), Decimal("0.0"), Decimal("0.00")),
                ),
                _day(date(2026, 8, 11)),
            )
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.COMPLETE
    assert movement.currency == "USD"
    assert movement.period == CURRENT
    assert tuple(day.source_date for day in movement.days) == (
        date(2026, 8, 10),
        date(2026, 8, 11),
        date(2026, 8, 12),
    )
    first = movement.days[0]
    assert (first.source_date, first.spend.value, first.spend.evidence) == (
        date(2026, 8, 10),
        Decimal("0"),
        AvailabilityState.AVAILABLE,
    )
    assert tuple((result.model_id, result.value) for result in first.model_results) == (
        (MODEL_IDS[0], Decimal("0")),
        (MODEL_IDS[1], Decimal("0.0")),
        (MODEL_IDS[2], Decimal("0.00")),
    )
    assert movement.gaps == ()
    assert movement.insufficiency_reasons == ()


def test_daily_partial_retains_every_gap_reason_with_date_field_and_model_custody() -> None:
    period = DateRange(date(2026, 8, 10), date(2026, 8, 14))
    movement = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(
                    date(2026, 8, 14),
                    ChannelDailyMetricValue(None, AvailabilityState.SUPPRESSED),
                ),
                _day(date(2026, 8, 10)),
                _day(
                    date(2026, 8, 13),
                    ChannelDailyMetricValue(None, AvailabilityState.EMPTY),
                    _daily_models(
                        (None, None, Decimal("3")),
                        (
                            AvailabilityState.SUPPRESSED,
                            AvailabilityState.EMPTY,
                            AvailabilityState.AVAILABLE,
                        ),
                    ),
                ),
                _day(date(2026, 8, 11)),
            ),
            period=period,
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.PARTIAL
    assert tuple(reason.value for reason in movement.insufficiency_reasons) == (
        "missing_date",
        "missing_spend",
        "suppressed_spend",
        "suppressed_model",
        "undefined_ratio",
    )
    assert tuple(
        (gap.source_date, gap.field.value, gap.model_id, gap.reason.value) for gap in movement.gaps
    ) == (
        (date(2026, 8, 12), "spend", None, "missing_date"),
        (date(2026, 8, 12), "model", MODEL_IDS[0], "missing_date"),
        (date(2026, 8, 12), "model", MODEL_IDS[1], "missing_date"),
        (date(2026, 8, 12), "model", MODEL_IDS[2], "missing_date"),
        (date(2026, 8, 13), "spend", None, "missing_spend"),
        (date(2026, 8, 13), "model", MODEL_IDS[0], "suppressed_model"),
        (date(2026, 8, 13), "model", MODEL_IDS[1], "undefined_ratio"),
        (date(2026, 8, 14), "spend", None, "suppressed_spend"),
    )
    missing_day = movement.days[2]
    assert (missing_day.source_date, missing_day.spend.value) == (date(2026, 8, 12), None)
    assert all(result.value is None for result in missing_day.model_results)


@pytest.mark.parametrize(
    "reason",
    [
        ChannelDailyReasonCode.UNSUPPORTED_PERIOD_SIZE,
        ChannelDailyReasonCode.ARTIFACT_OVERFLOW,
        ChannelDailyReasonCode.MISSING_PUBLICATION,
        ChannelDailyReasonCode.INCOMPATIBLE_SCOPE,
        ChannelDailyReasonCode.TRUNCATED,
        ChannelDailyReasonCode.QUERY_REFUSED,
    ],
)
def test_each_preflight_hard_reason_produces_insufficient(
    reason: ChannelDailyReasonCode,
) -> None:
    movement = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(date(2026, 8, 10)),
                _day(date(2026, 8, 11)),
                _day(date(2026, 8, 12)),
            ),
            hard_reasons=(reason,),
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.INSUFFICIENT
    assert tuple(item.value for item in movement.insufficiency_reasons) == (reason.value,)
    assert movement.period == CURRENT


def test_partial_cell_evidence_derives_truncated_hard_reason() -> None:
    movement = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(
                    date(2026, 8, 10),
                    ChannelDailyMetricValue(Decimal("10"), AvailabilityState.PARTIAL),
                ),
                _day(date(2026, 8, 11)),
                _day(date(2026, 8, 12)),
            )
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.INSUFFICIENT
    assert tuple(reason.value for reason in movement.insufficiency_reasons) == ("truncated",)


def test_duplicate_source_date_derives_incompatible_scope_without_row_order_choice() -> None:
    duplicate_a = _day(date(2026, 8, 10), spend=Decimal("10"))
    duplicate_b = _day(date(2026, 8, 10), spend=Decimal("99"))
    remaining = (_day(date(2026, 8, 11)), _day(date(2026, 8, 12)))

    first = reduce_channel_daily_movement(_daily_request((duplicate_a, duplicate_b, *remaining)))
    second = reduce_channel_daily_movement(
        _daily_request((duplicate_b, duplicate_a, *reversed(remaining)))
    )

    assert first == second
    assert first.outcome is ChannelDailyMovementOutcome.INSUFFICIENT
    assert first.insufficiency_reasons[0].code is ChannelDailyReasonCode.INCOMPATIBLE_SCOPE
    assert first.days[0].spend.value is None
    assert first.gaps[0].field is ChannelDailyGapField.SPEND
    assert first.gaps[0].reason is ChannelDailyReasonCode.MISSING_DATE


def test_one_source_date_is_too_few_and_each_model_lacks_a_usable_series() -> None:
    one_day = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    movement = reduce_channel_daily_movement(_daily_request((_day(one_day.start),), period=one_day))

    assert movement.outcome is ChannelDailyMovementOutcome.INSUFFICIENT
    assert tuple(reason.value for reason in movement.insufficiency_reasons) == (
        "too_few_dates",
        f"no_usable_model_series:{MODEL_IDS[0]}",
        f"no_usable_model_series:{MODEL_IDS[1]}",
        f"no_usable_model_series:{MODEL_IDS[2]}",
    )
    assert movement.period == one_day


def test_no_usable_spend_series_is_hard_while_missing_spend_gaps_are_retained() -> None:
    movement = reduce_channel_daily_movement(
        _daily_request(
            tuple(
                _day(
                    source_date,
                    ChannelDailyMetricValue(None, AvailabilityState.EMPTY),
                )
                for source_date in (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12))
            )
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.INSUFFICIENT
    assert tuple(reason.value for reason in movement.insufficiency_reasons) == (
        "no_usable_spend_series",
        "missing_spend",
    )
    assert tuple((gap.source_date, gap.field.value) for gap in movement.gaps) == (
        (date(2026, 8, 10), "spend"),
        (date(2026, 8, 11), "spend"),
        (date(2026, 8, 12), "spend"),
    )


def test_no_usable_model_series_is_reported_per_model_in_registry_order() -> None:
    movement = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(
                    date(2026, 8, 10),
                    model_values=_daily_models(
                        (Decimal("1"), None, Decimal("3")),
                        (
                            AvailabilityState.AVAILABLE,
                            AvailabilityState.EMPTY,
                            AvailabilityState.AVAILABLE,
                        ),
                    ),
                ),
                _day(
                    date(2026, 8, 11),
                    model_values=_daily_models(
                        (None, None, Decimal("3")),
                        (
                            AvailabilityState.EMPTY,
                            AvailabilityState.SUPPRESSED,
                            AvailabilityState.AVAILABLE,
                        ),
                    ),
                ),
                _day(
                    date(2026, 8, 12),
                    model_values=_daily_models(
                        (None, None, Decimal("3")),
                        (
                            AvailabilityState.EMPTY,
                            AvailabilityState.EMPTY,
                            AvailabilityState.AVAILABLE,
                        ),
                    ),
                ),
            )
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.INSUFFICIENT
    assert tuple(reason.value for reason in movement.insufficiency_reasons) == (
        f"no_usable_model_series:{MODEL_IDS[0]}",
        f"no_usable_model_series:{MODEL_IDS[1]}",
        "suppressed_model",
        "undefined_ratio",
    )
    assert movement.currency == "USD"
    assert movement.period == CURRENT


def test_all_daily_reasons_retain_documented_precedence_even_when_input_is_reversed() -> None:
    one_day = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    hard_reasons = tuple(
        reversed(
            (
                ChannelDailyReasonCode.UNSUPPORTED_PERIOD_SIZE,
                ChannelDailyReasonCode.ARTIFACT_OVERFLOW,
                ChannelDailyReasonCode.MISSING_PUBLICATION,
                ChannelDailyReasonCode.INCOMPATIBLE_SCOPE,
                ChannelDailyReasonCode.TRUNCATED,
                ChannelDailyReasonCode.QUERY_REFUSED,
            )
        )
    )
    movement = reduce_channel_daily_movement(
        _daily_request((), period=one_day, hard_reasons=hard_reasons)
    )

    assert tuple(reason.value for reason in movement.insufficiency_reasons) == (
        "unsupported_period_size",
        "artifact_overflow",
        "missing_publication",
        "incompatible_scope",
        "truncated",
        "query_refused",
        "too_few_dates",
        "no_usable_spend_series",
        f"no_usable_model_series:{MODEL_IDS[0]}",
        f"no_usable_model_series:{MODEL_IDS[1]}",
        f"no_usable_model_series:{MODEL_IDS[2]}",
        "missing_date",
    )
    assert movement.outcome is ChannelDailyMovementOutcome.INSUFFICIENT


def test_qualifier_prevents_complete_even_when_every_value_is_present() -> None:
    movement = reduce_channel_daily_movement(
        replace(
            _daily_request(
                (
                    _day(date(2026, 8, 10)),
                    _day(date(2026, 8, 11)),
                    _day(date(2026, 8, 12)),
                )
            ),
            qualifiers=("Fictional evidence qualifier.",),
        )
    )

    assert movement.outcome is ChannelDailyMovementOutcome.PARTIAL
    assert movement.qualifiers == ("Fictional evidence qualifier.",)
    assert movement.gaps == ()
    assert movement.insufficiency_reasons == ()


def test_missing_publication_closes_one_period_without_fabricating_rows() -> None:
    period = channel_attribution_robustness(
        ChannelRobustnessRequest(
            page=None,
            period=CURRENT,
            missing_publication=True,
        )
    )

    assert period.outcome is ChannelRobustnessOutcome.INSUFFICIENT
    assert period.insufficiency_reasons == (ChannelInsufficiencyReason.MISSING_PUBLICATION,)
    assert period.rows == ()
    assert period.marketplace_key is None
    assert period.currency is None


def test_v2_requires_immediately_preceding_equal_length_periods() -> None:
    current = _period(ChannelRobustnessOutcome.ROBUST_LEADER)
    non_adjacent = _period(
        ChannelRobustnessOutcome.ROBUST_LEADER,
        period=DateRange(date(2026, 8, 6), date(2026, 8, 8)),
    )

    with pytest.raises(ValueError, match="immediately precede"):
        channel_attribution_robustness_v2(current, non_adjacent)


def test_v2_composition_never_recalculates_weekly_values_from_daily_ratios() -> None:
    current = _period(ChannelRobustnessOutcome.ROBUST_LEADER)
    comparison = _period(ChannelRobustnessOutcome.ROBUST_LEADER, period=COMPARISON)
    daily = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(date(2026, 8, 10), model_values=(Decimal("1"),) * 3),
                _day(date(2026, 8, 11), model_values=(Decimal("100"),) * 3),
                _day(date(2026, 8, 12), model_values=(Decimal("7"),) * 3),
            )
        )
    )

    artifact = channel_attribution_robustness_v2(current, comparison, daily)

    assert artifact.current is current
    assert artifact.comparison is comparison
    assert artifact.selected_channel_daily is daily
    assert artifact.current.rows[0].model_results[0].value == Decimal("2")
    assert artifact.current.period == CURRENT
    assert artifact.period_relation is ChannelPeriodRelation.LEADER_PERSISTED


def test_v2_and_daily_payloads_are_canonical_selector_safe_and_decimal_exact() -> None:
    current = _period(ChannelRobustnessOutcome.ROBUST_LEADER)
    comparison = _period(ChannelRobustnessOutcome.ROBUST_LEADER, period=COMPARISON)
    daily = reduce_channel_daily_movement(
        _daily_request(
            (
                _day(
                    date(2026, 8, 10),
                    spend=Decimal("0.00"),
                    model_values=(Decimal("0"), Decimal("1.20"), Decimal("2.300")),
                ),
                _day(date(2026, 8, 11)),
                _day(date(2026, 8, 12)),
            )
        )
    )
    artifact = ChannelAttributionRobustnessV2(
        current=current,
        comparison=comparison,
        period_relation=ChannelPeriodRelation.LEADER_PERSISTED,
        selected_channel_daily=daily,
    )

    payload = channel_attribution_robustness_v2_payload(artifact, LABELS)

    assert set(payload) == {
        "boundaries",
        "comparison",
        "current",
        "period_relation",
        "selected_channel_daily",
    }
    assert "boundaries" not in cast("dict[str, object]", payload["current"])
    daily_payload = cast("dict[str, object]", payload["selected_channel_daily"])
    first_day = cast("dict[str, object]", cast("list[object]", daily_payload["days"])[0])
    assert first_day["source_date"] == "2026-08-10"
    assert cast("dict[str, object]", first_day["spend"])["value"] == "0.00"
    model_results = cast("dict[str, dict[str, object]]", first_day["model_results"])
    assert tuple(model_results[model_id]["value"] for model_id in MODEL_IDS) == (
        "0",
        "1.20",
        "2.300",
    )
    assert daily_payload["channel"] == "Fictional A"
    assert daily_payload["marketplace"] == "Fictional marketplace"
    _assert_selector_safe(payload)


def test_v2_parent_decoder_returns_only_typed_parent_selection_fields() -> None:
    artifact = channel_attribution_robustness_v2(
        _period(ChannelRobustnessOutcome.ROBUST_LEADER),
        _period(ChannelRobustnessOutcome.ROBUST_LEADER, period=COMPARISON),
    )

    decoded = decode_channel_v2_parent(channel_attribution_robustness_v2_payload(artifact, LABELS))

    assert decoded.current_period == CURRENT
    assert decoded.comparison_period == COMPARISON
    assert decoded.models == MODEL_IDS
    assert decoded.marketplace == "Fictional marketplace"
    assert decoded.currency == "USD"
    assert tuple(row.public_label for row in decoded.current_rows) == (
        "Fictional A",
        "Fictional B",
    )


_V2_PARENT_MUTATIONS: tuple[Callable[[dict[str, object]], object], ...] = (
    lambda payload: payload.pop("boundaries"),
    lambda payload: payload.__setitem__("selected_channel_daily", {}),
    lambda payload: cast("dict[str, object]", payload["current"]).__setitem__(
        "models", ["first_touch_v1", "first_touch_v1"]
    ),
    lambda payload: cast(
        "dict[str, object]",
        cast(
            "list[object]",
            cast("dict[str, object]", payload["current"])["rows"],
        )[1],
    ).__setitem__("channel", "Fictional A"),
    lambda payload: cast("dict[str, object]", payload["comparison"]).__setitem__(
        "period", {"start": "2026-08-01", "end": "2026-08-03"}
    ),
)


@pytest.mark.parametrize("mutate", _V2_PARENT_MUTATIONS)
def test_v2_parent_decoder_rejects_malformed_parent_shapes(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    artifact = channel_attribution_robustness_v2(
        _period(ChannelRobustnessOutcome.ROBUST_LEADER),
        _period(ChannelRobustnessOutcome.ROBUST_LEADER, period=COMPARISON),
    )
    payload = channel_attribution_robustness_v2_payload(artifact, LABELS)

    mutate(payload)

    with pytest.raises(ValueError):
        decode_channel_v2_parent(payload)


def test_v2_parent_decoder_accepts_insufficient_parent_without_current_rows() -> None:
    artifact = channel_attribution_robustness_v2(
        _period(ChannelRobustnessOutcome.INSUFFICIENT),
        _period(ChannelRobustnessOutcome.INSUFFICIENT, period=COMPARISON),
    )
    payload = channel_attribution_robustness_v2_payload(artifact, LABELS)
    current = cast("dict[str, object]", payload["current"])
    current["rows"] = []

    decoded = decode_channel_v2_parent(payload)

    assert decoded.current_period == CURRENT
    assert decoded.current_rows == ()
