# SPDX-License-Identifier: MIT-0
"""The seven boundary schemas between the input builder, the job and the publisher (design §4e).

Each is a pydantic model with `extra="forbid"` and `frozen=True`. `boundary_schema_digest(model)`
is the SHA-256 of the canonical JSON of its `model_json_schema()`; the seven digests sit in the
manifest, the run report and the run fingerprint, and `make check-forecast-schemas` fails when
`contracts/generated/demand-forecast-boundaries.json` drifts from them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from domain.forecasting.models import ModelName, SkipReason, WeekState
from domain.forecasting.policy import MAX_SHARDS, TARGETS

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
AttemptId = Annotated[str, StringConstraints(pattern=r"^r[0-9]+a[0-9]+$")]
DecimalString = Annotated[str, StringConstraints(pattern=r"^-?[0-9]+\.[0-9]{4}$")]
CanonicalAsin = Annotated[str, StringConstraints(pattern=r"^amazon_asin:[a-z0-9]{10}$")]
DisplayAsin = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{10}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Target = Literal["units_shipped", "units_ordered", "revenue_shipped", "cogs_shipped"]
Origin = Literal["publishable", "not_publishable", "not_attempted"]
DayCount = Annotated[int, Field(ge=0, le=7)]
UNIT_TARGETS: tuple[Target, ...] = ("units_shipped", "units_ordered")


class _Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)


class PinV1(_Boundary):
    """One pinned CURRENT publication (the port's `PinnedPublication`, mapped by the builder)."""

    partition: NonEmpty
    publication_id: NonEmpty
    fencing_token: int
    digest: Hex64


class ShardV1(_Boundary):
    """One shard of the sorted roster (design §3c)."""

    index: Annotated[int, Field(ge=0, lt=MAX_SHARDS)]
    asin_count: Annotated[int, Field(ge=1, le=2000)]
    first_canonical_asin: CanonicalAsin
    last_canonical_asin: CanonicalAsin


class ForecastSetV1(_Boundary):
    """The one forecast set (design §3a)."""

    marketplace_key: NonEmpty
    distributor_view: NonEmpty
    selling_program: NonEmpty


class RunIdentityV1(_Boundary):
    """Every run fingerprint component (design §4b)."""

    input_fingerprint: Hex64
    policy_sha256: Hex64
    policy_section_versions: dict[str, int]
    processor_code_identity: NonEmpty
    lock_digest: Hex64
    model_id: NonEmpty
    model_artifact_digest: Hex64
    boundary_schema_digests: dict[str, Hex64]
    instance_type: NonEmpty
    max_runtime_seconds: Annotated[int, Field(ge=1)]


class PinsV1(_Boundary):
    """The window's sales and traffic pins and the product pins."""

    sales: tuple[PinV1, ...]
    traffic: tuple[PinV1, ...]
    products: tuple[PinV1, ...]


class ParquetObjectV1(_Boundary):
    """One `UNLOAD` output object, its digest and row count."""

    key: NonEmpty
    sha256: Hex64
    rows: Annotated[int, Field(ge=0)]
    kind: Literal["target", "traffic"]
    shard: Annotated[int, Field(ge=0, lt=MAX_SHARDS)]


class IntervalV1(_Boundary):
    """An inclusive covered day range."""

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> IntervalV1:
        if self.end < self.start:
            raise ValueError("an interval's end precedes its start")
        return self


class CoverageV1(_Boundary):
    """The covered days of the sales and traffic pins, as intervals."""

    sales: tuple[IntervalV1, ...]
    traffic: tuple[IntervalV1, ...]


class InputManifestV1(_Boundary):
    """What the input builder staged for one build attempt (input builder → job, publisher)."""

    schema_version: Literal[1]
    run_id: NonEmpty
    build_attempt_id: AttemptId
    forecast_set: ForecastSetV1 = Field(alias="set")
    input_end: date
    expected_fence: Annotated[int, Field(ge=0)]
    input_fingerprint: Hex64
    run_fingerprint: Hex64
    run_identity: RunIdentityV1
    pins: PinsV1
    shards: tuple[ShardV1, ...]
    parquet_objects: tuple[ParquetObjectV1, ...]
    titles_key: NonEmpty
    titles_sha256: Hex64
    coverage: CoverageV1
    newest_publication_published_at: AwareDatetime
    boundary_schema_digests: dict[str, Hex64]

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _shards_contiguous(self) -> InputManifestV1:
        if not self.shards:
            raise ValueError("a manifest names at least one shard")
        if [shard.index for shard in self.shards] != list(range(len(self.shards))):
            raise ValueError("shard indexes must be contiguous from 0")
        if self.input_fingerprint != self.run_identity.input_fingerprint:
            raise ValueError("input_fingerprint differs from run_identity.input_fingerprint")
        return self


class WeeklyTargetAggregateRowV1(_Boundary):
    """One `UNLOAD` row of weekly target aggregates (Athena → job)."""

    schema_version: Literal[1]
    canonical_asin: CanonicalAsin
    week_start: date
    target: Target
    value_sum: Decimal | None
    days_with_row: DayCount
    days_with_null: DayCount
    min_currency: str | None
    max_currency: str | None

    @model_validator(mode="after")
    def _nulls_within_rows(self) -> WeeklyTargetAggregateRowV1:
        if self.days_with_null > self.days_with_row:
            raise ValueError("days_with_null exceeds days_with_row")
        return self


class WeeklyTrafficAggregateRowV1(_Boundary):
    """One `UNLOAD` row of weekly glance views (Athena → job)."""

    schema_version: Literal[1]
    canonical_asin: CanonicalAsin
    week_start: date
    glance_views_sum: int | None
    days_with_traffic_row: DayCount


class TitleEntryV1(_Boundary):
    """One product's title from the forecast marketplace's hierarchy (null when unmapped)."""

    canonical_asin: CanonicalAsin
    display_asin: DisplayAsin
    title: str | None


class TitleMapV1(_Boundary):
    """Every roster product's title, sorted by canonical ASIN, each once (builder → publisher)."""

    schema_version: Literal[1]
    entries: tuple[TitleEntryV1, ...]

    @model_validator(mode="after")
    def _sorted_unique(self) -> TitleMapV1:
        keys = [entry.canonical_asin for entry in self.entries]
        if keys != sorted(set(keys)):
            raise ValueError("title entries must be sorted by canonical_asin, each once")
        return self


class BacktestScoreV1(_Boundary):
    """A model's holdout score on the common scored week set (design §5b)."""

    wql: DecimalString
    mase: DecimalString | None
    mase_gap: DecimalString | None
    weeks_in_range: Annotated[int, Field(ge=0)]
    weeks_scored: Annotated[int, Field(ge=0)]


class ModelEvaluationV1(_Boundary):
    """One model's backtest, validity and origin status for one target."""

    model: ModelName
    backtest_score: BacktestScoreV1 | None
    validity_reason: SkipReason | None
    origin_status: Origin
    origin_failure: str | None


class BandsV1(_Boundary):
    """A shown forecast: 13 stored decimal strings per quantile."""

    p10: Annotated[tuple[DecimalString, ...], Field(min_length=13, max_length=13)]
    p50: Annotated[tuple[DecimalString, ...], Field(min_length=13, max_length=13)]
    p90: Annotated[tuple[DecimalString, ...], Field(min_length=13, max_length=13)]


class TargetCandidateV1(_Boundary):
    """One target's chosen forecast or withheld reason, with all three evaluations."""

    chosen_model: ModelName | None
    withheld_reason: SkipReason | None
    chosen_forecast: BandsV1 | None
    gate_reason: str | None
    scored_weeks: Annotated[int, Field(ge=0)]
    evaluations: tuple[ModelEvaluationV1, ...]

    @model_validator(mode="after")
    def _either_or(self) -> TargetCandidateV1:
        if (self.chosen_model is None) == (self.withheld_reason is None):
            raise ValueError("exactly one of chosen_model and withheld_reason is set")
        if (self.chosen_forecast is None) != (self.chosen_model is None):
            raise ValueError("chosen_forecast is set exactly when chosen_model is")
        models = sorted(evaluation.model.value for evaluation in self.evaluations)
        if models != sorted(model.value for model in ModelName):
            raise ValueError("exactly one evaluation each for chronos2, seasonal_naive and ets")
        return self


class RecentTargetWeekV1(_Boundary):
    """One target's state and value in one recent week."""

    state: WeekState
    value: DecimalString | None

    @model_validator(mode="after")
    def _value_with_state(self) -> RecentTargetWeekV1:
        if (self.value is None) != (self.state is WeekState.UNKNOWN):
            raise ValueError("a recent week has a value exactly when it is not unknown")
        return self


class RecentWeekV1(_Boundary):
    """One of the last 13 weeks, with the historical shipped-below-ordered flag (design §3b)."""

    week_start: date
    targets: dict[Target, RecentTargetWeekV1]
    stockout: bool | None

    @model_validator(mode="after")
    def _stockout_rule(self) -> RecentWeekV1:
        if set(self.targets) != set(TARGETS):
            raise ValueError("a recent week carries every target")
        both_observed = all(self.targets[t].state is WeekState.OBSERVED for t in UNIT_TARGETS)
        if (self.stockout is None) == both_observed:
            raise ValueError("stockout is set exactly when both unit targets are observed")
        return self


class WeekStateCountsV1(_Boundary):
    """Counts of context weeks per state."""

    observed: Annotated[int, Field(ge=0)]
    inferred_zero: Annotated[int, Field(ge=0)]
    unknown: Annotated[int, Field(ge=0)]


class CandidateSeriesV1(_Boundary):
    """One ASIN's candidate from one shard attempt (job → publisher)."""

    schema_version: Literal[1]
    canonical_asin: CanonicalAsin
    display_asin: DisplayAsin
    shard_index: Annotated[int, Field(ge=0, lt=MAX_SHARDS)]
    targets: dict[Target, TargetCandidateV1]
    recent_weeks: Annotated[tuple[RecentWeekV1, ...], Field(min_length=13, max_length=13)]
    week_state_counts: WeekStateCountsV1
    stockout_weeks_last_13: Annotated[int, Field(ge=0, le=13)]
    currency_code: str | None

    @model_validator(mode="after")
    def _whole(self) -> CandidateSeriesV1:
        if set(self.targets) != set(TARGETS):
            raise ValueError("a candidate carries exactly one entry per target")
        starts = [week.week_start for week in self.recent_weeks]
        if starts != sorted(starts) or len(set(starts)) != len(starts):
            raise ValueError("recent weeks are oldest first, each once")
        flagged = sum(1 for week in self.recent_weeks if week.stockout is True)
        if flagged != self.stockout_weeks_last_13:
            raise ValueError("stockout_weeks_last_13 differs from the recent weeks")
        return self


class RunReportV1(_Boundary):
    """One shard attempt's run report (job → publisher)."""

    schema_version: Literal[1]
    run_id: NonEmpty
    shard_index: Annotated[int, Field(ge=0, lt=MAX_SHARDS)]
    asin_count: Annotated[int, Field(ge=0)]
    attempt_id: AttemptId
    input_attempt_id: AttemptId
    series_sha256: Hex64
    counts_by_reason: dict[str, Annotated[int, Field(ge=0)]]
    week_state_counts: WeekStateCountsV1
    inferred_zero_days: Annotated[int, Field(ge=0)]
    durations_ms: dict[str, Annotated[int, Field(ge=0)]]
    policy_section_versions: dict[str, int]
    policy_sha256: Hex64
    image_identity: NonEmpty
    lock_digest: Hex64
    model_id: NonEmpty
    model_artifact_digest: Hex64
    boundary_schema_digests: dict[str, Hex64]
    run_fingerprint: Hex64


class RosterTargetV1(_Boundary):
    """One target's list-row summary."""

    chosen_model: ModelName | None
    withheld_reason: SkipReason | None
    p50_totals: dict[Literal["4", "8", "13"], DecimalString | None]
    currency_code: str | None


class RosterEntryV1(_Boundary):
    """One list row: a roster-part line (rank null) or a list-page entry (rank 1 to N)."""

    schema_version: Literal[1]
    rank: Annotated[int, Field(ge=1)] | None
    display_asin: DisplayAsin
    title: str | None
    title_truncated: bool
    targets: dict[Target, RosterTargetV1]
    accuracy_class: str | None
    stockout_weeks_last_13: Annotated[int, Field(ge=0, le=13)]


BOUNDARY_MODELS: tuple[type[BaseModel], ...] = (
    InputManifestV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
    TitleMapV1,
    CandidateSeriesV1,
    RunReportV1,
    RosterEntryV1,
)


def boundary_schema(model: type[BaseModel]) -> dict[str, object]:
    """Return the model's JSON Schema (by alias, validation mode)."""
    return model.model_json_schema(by_alias=True)


def boundary_schema_digest(model: type[BaseModel]) -> str:
    """SHA-256 of the canonical JSON of the model's JSON Schema."""
    canonical = json.dumps(boundary_schema(model), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def boundary_schema_digests() -> dict[str, str]:
    """Every boundary model's digest, keyed by model name."""
    return {model.__name__: boundary_schema_digest(model) for model in BOUNDARY_MODELS}
