# SPDX-License-Identifier: MIT-0
"""Forecast accuracy labels and the shared-currency refusal (ADR-163).

A wrong label tells a planner a forecast beat the naive baseline when it did not; a money answer
over two currencies would put unlike figures side by side. These tests pin each label from a
stored score and the refusal with its positive controls.
"""

from __future__ import annotations

import pytest

from domain.forecasting.boundary import RosterTargetV1, TargetCandidateV1
from domain.forecasting.summaries import classify_accuracy, require_shared_currency
from domain.shared_kernel.errors import DomainError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-163")]


def _candidate(mase: str | None, gap: str | None, *, scored: bool = True) -> TargetCandidateV1:
    score = {
        "wql": "0.2000",
        "mase": mase,
        "mase_gap": gap,
        "weeks_in_range": 9,
        "weeks_scored": 13,
    }
    evaluations = [
        {
            "model": model,
            "backtest_score": score if scored else None,
            "validity_reason": None,
            "origin_status": "publishable",
            "origin_failure": None,
        }
        for model in ("chronos2", "seasonal_naive", "ets")
    ]
    return TargetCandidateV1.model_validate(
        {
            "chosen_model": "ets",
            "withheld_reason": None,
            "chosen_forecast": {
                "p10": ["1.0000"] * 13,
                "p50": ["2.0000"] * 13,
                "p90": ["3.0000"] * 13,
            },
            "gate_reason": None,
            "scored_weeks": 13,
            "evaluations": evaluations,
        }
    )


@pytest.mark.parametrize(
    ("mase", "gap", "label"),
    [
        ("0.9999", None, "beat_naive"),
        ("1.0000", None, "not_beat_naive"),
        (None, "HISTORY_UNCHANGING", "history_unchanging"),
        (None, "MASE_SCALE_UNKNOWN", "scale_unknown"),
    ],
)
def test_the_chosen_models_stored_score_gets_its_label(
    mase: str | None, gap: str | None, label: str
) -> None:
    assert classify_accuracy(_candidate(mase, gap)) == label


def test_an_unscored_or_withheld_target_has_no_label() -> None:
    assert classify_accuracy(_candidate("0.5000", None, scored=False)) is None
    withheld = _candidate("0.5000", None).model_copy(
        update={
            "chosen_model": None,
            "withheld_reason": "INSUFFICIENT_CONTEXT",
            "chosen_forecast": None,
        }
    )
    assert classify_accuracy(withheld) is None


def _row(currency: str | None, *, shown: bool = True) -> RosterTargetV1:
    return RosterTargetV1(
        chosen_model="chronos2" if shown else None,  # pyright: ignore[reportArgumentType]
        withheld_reason=None if shown else "INSUFFICIENT_CONTEXT",  # pyright: ignore[reportArgumentType]
        p50_totals=dict.fromkeys(("4", "8", "13")),  # pyright: ignore[reportArgumentType]
        currency_code=currency,
    )


def test_a_money_target_over_two_shown_currencies_is_refused() -> None:
    with pytest.raises(DomainError) as refused:
        require_shared_currency("revenue_shipped", [_row("USD"), _row("EUR")])
    assert str(refused.value.code) == "ADLY-FORECAST_READ-0006"
    assert refused.value.context == {"currencies": ["EUR", "USD"]}


def test_one_currency_a_withheld_other_currency_and_unit_targets_pass() -> None:
    require_shared_currency("revenue_shipped", [_row("USD"), _row("USD")])
    require_shared_currency("cogs_shipped", [_row("USD"), _row("EUR", shown=False)])
    require_shared_currency("units_ordered", [_row("USD"), _row("EUR")])
