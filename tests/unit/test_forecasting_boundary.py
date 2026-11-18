# SPDX-License-Identifier: MIT-0
"""Boundary schemas between builder, job and publisher: a lax model would pass a bad forecast."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from domain.forecasting.boundary import (
    BOUNDARY_MODELS,
    CandidateSeriesV1,
    InputManifestV1,
    RecentWeekV1,
    RosterEntryV1,
    RunReportV1,
    TargetCandidateV1,
    TitleMapV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
    boundary_schema_digests,
)
from domain.forecasting.policy import TARGETS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
HEX = "a" * 64
ASIN = "amazon_asin:b0fixtur01"
DIGESTS = {"InputManifestV1": HEX}


def _evaluations() -> list[dict[str, Any]]:
    return [
        {
            "model": model,
            "backtest_score": None,
            "validity_reason": "INSUFFICIENT_CONTEXT",
            "origin_status": "not_attempted",
            "origin_failure": None,
        }
        for model in ("chronos2", "seasonal_naive", "ets")
    ]


def _target() -> dict[str, Any]:
    bands = ["1.0000"] * 13
    return {
        "chosen_model": "chronos2",
        "withheld_reason": None,
        "chosen_forecast": {"p10": bands, "p50": bands, "p90": bands},
        "gate_reason": None,
        "scored_weeks": 13,
        "evaluations": _evaluations(),
    }


def _week(start: date, stockout: bool | None = False) -> dict[str, Any]:
    return {
        "week_start": start.isoformat(),
        "targets": {t: {"state": "observed", "value": "2.0000"} for t in TARGETS},
        "stockout": stockout,
    }


SAMPLES: dict[type[BaseModel], dict[str, Any]] = {  # pyright: ignore[reportAssignmentType]
    InputManifestV1: {
        "schema_version": 1,
        "run_id": "run-1",
        "build_attempt_id": "r0a0",
        "set": {
            "marketplace_key": "amazon_us",
            "distributor_view": "MANUFACTURING",
            "selling_program": "RETAIL",
        },
        "input_end": "2026-10-03",
        "expected_fence": 0,
        "input_fingerprint": HEX,
        "run_fingerprint": HEX,
        "run_identity": {
            "input_fingerprint": HEX,
            "policy_sha256": HEX,
            "policy_section_versions": {"engine": 1},
            "processor_code_identity": HEX,
            "lock_digest": HEX,
            "model_id": "amazon/chronos-2",
            "model_artifact_digest": HEX,
            "boundary_schema_digests": DIGESTS,
            "instance_type": "ml.m5.xlarge",
            "max_runtime_seconds": 3600,
        },
        "pins": {
            "sales": [{"partition": "p", "publication_id": "x", "fencing_token": 1, "digest": HEX}],
            "traffic": [],
            "products": [],
        },
        "shards": [
            {
                "index": 0,
                "asin_count": 1,
                "first_canonical_asin": ASIN,
                "last_canonical_asin": ASIN,
            }
        ],
        "parquet_objects": [{"key": "k", "sha256": HEX, "rows": 4, "kind": "target", "shard": 0}],
        "titles_key": "titles.json",
        "titles_sha256": HEX,
        "coverage": {"sales": [{"start": "2024-10-06", "end": "2026-10-03"}], "traffic": []},
        "newest_publication_published_at": "2026-10-05T00:00:00Z",
        "boundary_schema_digests": DIGESTS,
    },
    WeeklyTargetAggregateRowV1: {
        "schema_version": 1,
        "canonical_asin": ASIN,
        "week_start": "2026-09-27",
        "target": "units_shipped",
        "value_sum": "12.0000",
        "days_with_row": 7,
        "days_with_null": 0,
        "min_currency": None,
        "max_currency": None,
    },
    WeeklyTrafficAggregateRowV1: {
        "schema_version": 1,
        "canonical_asin": ASIN,
        "week_start": "2026-09-27",
        "glance_views_sum": 40,
        "days_with_traffic_row": 7,
    },
    TitleMapV1: {
        "schema_version": 1,
        "entries": [{"canonical_asin": ASIN, "display_asin": "B0FIXTUR01", "title": "Product 1"}],
    },
    CandidateSeriesV1: {
        "schema_version": 1,
        "canonical_asin": ASIN,
        "display_asin": "B0FIXTUR01",
        "shard_index": 0,
        "targets": {t: _target() for t in TARGETS},
        "recent_weeks": [_week(date(2026, 7, 5) + timedelta(weeks=n)) for n in range(13)],
        "week_state_counts": {"observed": 104, "inferred_zero": 0, "unknown": 0},
        "stockout_weeks_last_13": 0,
        "currency_code": "USD",
    },
    RunReportV1: {
        "schema_version": 1,
        "run_id": "run-1",
        "shard_index": 0,
        "asin_count": 1,
        "attempt_id": "r0a0",
        "input_attempt_id": "r0a0",
        "series_sha256": HEX,
        "counts_by_reason": {},
        "week_state_counts": {"observed": 1, "inferred_zero": 0, "unknown": 0},
        "inferred_zero_days": 0,
        "durations_ms": {"forecast": 1},
        "policy_section_versions": {"engine": 1},
        "policy_sha256": HEX,
        "image_identity": HEX,
        "lock_digest": HEX,
        "model_id": "amazon/chronos-2",
        "model_artifact_digest": HEX,
        "boundary_schema_digests": DIGESTS,
        "run_fingerprint": HEX,
    },
    RosterEntryV1: {
        "schema_version": 1,
        "rank": None,
        "display_asin": "B0FIXTUR01",
        "title": None,
        "title_truncated": False,
        "targets": {
            t: {
                "chosen_model": "chronos2",
                "withheld_reason": None,
                "p50_totals": {"4": "1.0000", "8": "2.0000", "13": "3.0000"},
                "currency_code": None,
            }
            for t in TARGETS
        },
        "accuracy_class": None,
        "stockout_weeks_last_13": 0,
    },
}
WRONG_TYPE: dict[type[BaseModel], tuple[str, str]] = {
    InputManifestV1: ("expected_fence", "zero"),
    WeeklyTargetAggregateRowV1: ("days_with_row", "seven"),
    WeeklyTrafficAggregateRowV1: ("glance_views_sum", "many"),
    TitleMapV1: ("entries", "none"),
    CandidateSeriesV1: ("shard_index", "first"),
    RunReportV1: ("asin_count", "one"),
    RosterEntryV1: ("title_truncated", "maybe"),
}


@pytest.mark.parametrize("model", BOUNDARY_MODELS, ids=lambda m: m.__name__)
def test_each_sample_validates(model: type[BaseModel]) -> None:
    model.model_validate(SAMPLES[model])


@pytest.mark.parametrize("model", BOUNDARY_MODELS, ids=lambda m: m.__name__)
def test_an_extra_field_is_refused(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        model.model_validate({**SAMPLES[model], "unexpected": 1})


@pytest.mark.parametrize("model", BOUNDARY_MODELS, ids=lambda m: m.__name__)
def test_a_missing_field_is_refused(model: type[BaseModel]) -> None:
    sample = dict(SAMPLES[model])
    del sample["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        model.model_validate(sample)


@pytest.mark.parametrize("model", BOUNDARY_MODELS, ids=lambda m: m.__name__)
def test_a_wrong_type_is_refused(model: type[BaseModel]) -> None:
    field, value = WRONG_TYPE[model]
    with pytest.raises(ValidationError, match=field):
        model.model_validate({**SAMPLES[model], field: value})


def test_a_target_with_both_or_neither_choice_is_refused() -> None:
    both = {**_target(), "withheld_reason": "INSUFFICIENT_CONTEXT"}
    neither = {**_target(), "chosen_model": None, "chosen_forecast": None}
    for case in (both, neither):
        with pytest.raises(ValidationError, match="exactly one of chosen_model"):
            TargetCandidateV1.model_validate(case)


def test_a_target_with_a_repeated_evaluation_is_refused() -> None:
    evaluations = _evaluations()
    evaluations[2] = {**evaluations[2], "model": "chronos2"}
    with pytest.raises(ValidationError, match="exactly one evaluation each"):
        TargetCandidateV1.model_validate({**_target(), "evaluations": evaluations})


def test_a_stockout_flag_with_an_unknown_unit_week_is_refused() -> None:
    week = _week(date(2026, 7, 5), stockout=True)
    week["targets"]["units_ordered"] = {"state": "unknown", "value": None}
    with pytest.raises(ValidationError, match="stockout is set exactly when"):
        RecentWeekV1.model_validate(week)


def test_a_traffic_row_labelled_target_and_the_reverse_are_refused() -> None:
    with pytest.raises(ValidationError):
        WeeklyTargetAggregateRowV1.model_validate(SAMPLES[WeeklyTrafficAggregateRowV1])
    with pytest.raises(ValidationError):
        WeeklyTrafficAggregateRowV1.model_validate(SAMPLES[WeeklyTargetAggregateRowV1])


def test_the_generated_file_matches_the_models() -> None:
    generated = json.loads(
        (ROOT / "contracts/generated/demand-forecast-boundaries.json").read_text()
    )
    assert {name: entry["digest"] for name, entry in generated["schemas"].items()} == (
        boundary_schema_digests()
    )
