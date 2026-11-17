# SPDX-License-Identifier: MIT-0
"""The demand forecast read API contract and its pure builders (ADR-163).

Two read-only routes, `GET /api/demand-forecast` and `GET /api/demand-forecast/status`, and the
four chat tools serve one read layer. This module owns what they return. Callers may rely on:

- every figure is a stored four-place value and its server `display` (ADR-084): a whole number
  for unit targets, two places (half up) for money targets. Nothing else is computed;
- the 4-, 8- and 13-week p50 totals are the publisher's stored roster row, never a read-time sum;
- each builder takes parsed stored views plus `now` and checks everything that needs two reads
  (page against `page_count`, entries against `entry_count`), raising a coded error;
- `contracts/generated/demand-forecast-api.json` is generated from `API_MODELS`, `API_ROUTES`
  and `API_ENUMS` by `make generate-forecast-schemas`. These models are kept apart from the
  boundary models, so they never enter a run fingerprint.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from domain.forecasting import list_pages
from domain.forecasting.boundary import (
    CandidateSeriesV1,
    DecimalString,
    DisplayAsin,
    NonEmpty,
    RosterEntryV1,
    RosterTargetV1,
    Target,
    TargetCandidateV1,
    WeekStateCountsV1,
)
from domain.forecasting.identity import parse_request_asin
from domain.forecasting.models import AccuracyGap, ModelName, SkipReason, WeekState
from domain.forecasting.numeric import display_whole
from domain.forecasting.policy import MONEY_TARGETS, TARGETS, WEEKDAYS, ServingPolicy
from domain.forecasting.publish import RETRY_EXHAUSTED, CurrentPointer, HeldCheck, RunOutcome
from domain.forecasting.selection import HeldCategory, current_age_days
from domain.forecasting.summaries import CURRENCY_MISMATCH, AccuracyClass, classify_accuracy
from domain.presentation.currency_display import currency_display_value
from domain.shared_kernel.errors import (
    AdlyticsError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)

# --- Codes ----------------------------------------------------------------------------------

ASIN_INVALID: Final = ErrorCode("FORECAST_READ", 1)
STATUS_INVALID: Final = ErrorCode("FORECAST_READ", 2)
PAGE_INVALID: Final = ErrorCode("FORECAST_READ", 3)
QUERY_INVALID: Final = ErrorCode("FORECAST_READ", 4)
FORECAST_ARGUMENT_INVALID: Final = ErrorCode("FORECAST_READ", 5)
# CURRENCY_MISMATCH is ErrorCode("FORECAST_READ", 6), owned by summaries.require_shared_currency.
FORECAST_NOT_PUBLISHED: Final = ErrorCode("FORECAST_READ", 7)
FORECAST_STORE_INCOMPLETE: Final = ErrorCode("FORECAST_READ", 8)
FORECAST_STORE_UNAVAILABLE: Final = ErrorCode("FORECAST_READ", 9)
ROUTE_UNKNOWN: Final = ErrorCode("FORECAST_READ", 10)
FORECAST_READ_FAILED: Final = ErrorCode("FORECAST_READ", 11)
IDENTITY_REQUIRED: Final = ErrorCode("FORECAST_READ", 12)
"""The authorizer projected no session subject; identity errors carry no code of their own."""


class ForecastReadRefusal(StrEnum):
    """The closed `refused` names a caller can branch on."""

    ASIN_INVALID = "ASIN_INVALID"
    STATUS_INVALID = "STATUS_INVALID"
    PAGE_INVALID = "PAGE_INVALID"
    QUERY_INVALID = "QUERY_INVALID"
    FORECAST_ARGUMENT_INVALID = "FORECAST_ARGUMENT_INVALID"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    FORECAST_NOT_PUBLISHED = "FORECAST_NOT_PUBLISHED"
    FORECAST_STORE_INCOMPLETE = "FORECAST_STORE_INCOMPLETE"
    FORECAST_STORE_UNAVAILABLE = "FORECAST_STORE_UNAVAILABLE"
    ROUTE_UNKNOWN = "ROUTE_UNKNOWN"
    FORECAST_READ_FAILED = "FORECAST_READ_FAILED"
    IDENTITY_REQUIRED = "IDENTITY_REQUIRED"


REFUSAL_BY_CODE: Final[Mapping[str, ForecastReadRefusal]] = {
    str(ASIN_INVALID): ForecastReadRefusal.ASIN_INVALID,
    str(STATUS_INVALID): ForecastReadRefusal.STATUS_INVALID,
    str(PAGE_INVALID): ForecastReadRefusal.PAGE_INVALID,
    str(QUERY_INVALID): ForecastReadRefusal.QUERY_INVALID,
    str(FORECAST_ARGUMENT_INVALID): ForecastReadRefusal.FORECAST_ARGUMENT_INVALID,
    str(CURRENCY_MISMATCH): ForecastReadRefusal.CURRENCY_MISMATCH,
    str(FORECAST_NOT_PUBLISHED): ForecastReadRefusal.FORECAST_NOT_PUBLISHED,
    str(FORECAST_STORE_INCOMPLETE): ForecastReadRefusal.FORECAST_STORE_INCOMPLETE,
    str(FORECAST_STORE_UNAVAILABLE): ForecastReadRefusal.FORECAST_STORE_UNAVAILABLE,
    str(ROUTE_UNKNOWN): ForecastReadRefusal.ROUTE_UNKNOWN,
    str(FORECAST_READ_FAILED): ForecastReadRefusal.FORECAST_READ_FAILED,
    str(IDENTITY_REQUIRED): ForecastReadRefusal.IDENTITY_REQUIRED,
}
"""Every forecast read code by its string."""

# --- Shared types ---------------------------------------------------------------------------

SchemaVersion = Literal[1]
Instant = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]
"""A whole-second UTC instant with `Z` (M3)."""
Display = Annotated[str, StringConstraints(pattern=r"^-?[0-9]+(\.[0-9]{2})?$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ViewKey = Literal["4", "8", "13"]
VIEW_KEYS: Final[frozenset[str]] = frozenset(get_args(ViewKey))

FreshnessState = Literal["fresh", "stale", "unavailable"]
FreshnessCause = Literal["age_over_limit", "last_run_failed", "last_run_held"]
FailureFamily = Literal[
    "quality_check", "input_not_ready", "forecast_job", "input_build", "retry_exhausted", "other"
]
Hold = Literal["none", "quality"]
LastRunOutcome = Literal["published", "held_quality", "failed", "rolled_back"]
DetailOutcome = Literal["selected", "not_forecast", "not_in_forecast", "unavailable"]
ListOutcome = Literal["complete", "unavailable"]


class _Api(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FigureV1(_Api):
    """One stored figure and its server display (ADR-084)."""

    value: DecimalString
    display: Display


class ProvenanceV1(_Api):
    """Which generation answered."""

    generation_id: NonEmpty
    published_at: Instant
    input_end: date
    first_forecast_week: date


class FreshnessV1(_Api):
    """REQ-6.3: fresh, stale with its causes, or unavailable."""

    state: FreshnessState
    age_days: Annotated[int, Field(ge=0)] | None
    stale_after_days: Annotated[int, Field(ge=1)]
    causes: tuple[FreshnessCause, ...]

    @model_validator(mode="after")
    def _rules(self) -> FreshnessV1:
        if (self.age_days is None) != (self.state == "unavailable"):
            raise ValueError("age_days is None exactly when freshness is unavailable")
        if bool(self.causes) != (self.state == "stale"):
            raise ValueError("causes are listed exactly when stale")
        if list(self.causes) != sorted(set(self.causes)):
            raise ValueError("causes are sorted, each once")
        return self


class HorizonV1(_Api):
    """The forecast weeks and the views the page may offer."""

    grain: Literal["week"]
    week_starts_on: Literal["sunday"]
    weeks: Annotated[tuple[date, ...], Field(min_length=13, max_length=13)]
    views: tuple[int, ...]
    default_view_weeks: int

    @model_validator(mode="after")
    def _default_in_views(self) -> HorizonV1:
        if self.default_view_weeks not in self.views:
            raise ValueError("default_view_weeks is one of views")
        return self


class AccuracyV1(_Api):
    """The chosen model's stored backtest score, with its label."""

    wql: DecimalString
    mase: DecimalString | None
    mase_gap: AccuracyGap | None
    weeks_in_range: Annotated[int, Field(ge=0)]
    weeks_scored: Annotated[int, Field(ge=0)]
    accuracy_class: AccuracyClass


def _check_target(  # noqa: PLR0913 -- one target's shape rule over its keyword parts
    target: str,
    *,
    chosen_model: ModelName | None,
    withheld_reason: SkipReason | None,
    currency_code: str | None,
    p50_totals: Mapping[ViewKey, FigureV1 | None],
    figures: tuple[FigureV1, ...],
) -> None:
    if (chosen_model is None) == (withheld_reason is None):
        raise ValueError(f"{target}: exactly one of chosen_model and withheld_reason is set")
    if {str(view) for view in p50_totals} != VIEW_KEYS:
        raise ValueError(f"{target}: p50_totals keys are exactly 4, 8 and 13")
    shown = chosen_model is not None
    totals = [total for total in p50_totals.values() if total is not None]
    if (len(totals) == len(VIEW_KEYS)) != shown or (not shown and totals):
        raise ValueError(f"{target}: p50_totals are set exactly when shown")
    money = target in MONEY_TARGETS
    if not money and currency_code is not None:
        raise ValueError(f"{target}: a unit target carries no currency")
    if money and shown and currency_code is None:
        raise ValueError(f"{target}: a shown money target carries its currency")
    for figure in (*totals, *figures):
        if ("." in figure.display) != money:
            raise ValueError(f"{target}: display has two places exactly for money targets")


def _check_targets(targets: Collection[str]) -> None:
    if set(targets) != set(TARGETS):
        raise ValueError("targets carry exactly the four forecast targets")


# --- Detail ---------------------------------------------------------------------------------


class BandsOutV1(_Api):
    """The weekly p10, p50 and p90, in stored order."""

    p10: Annotated[tuple[FigureV1, ...], Field(min_length=13, max_length=13)]
    p50: Annotated[tuple[FigureV1, ...], Field(min_length=13, max_length=13)]
    p90: Annotated[tuple[FigureV1, ...], Field(min_length=13, max_length=13)]


class TargetDetailV1(_Api):
    """One target of one product."""

    chosen_model: ModelName | None
    withheld_reason: SkipReason | None
    currency_code: CurrencyCode | None
    bands: BandsOutV1 | None
    p50_totals: dict[ViewKey, FigureV1 | None]
    accuracy: AccuracyV1 | None


class RecentTargetOutV1(_Api):
    """One target in one recent week."""

    state: WeekState
    value: FigureV1 | None

    @model_validator(mode="after")
    def _value_with_state(self) -> RecentTargetOutV1:
        if (self.value is None) != (self.state is WeekState.UNKNOWN):
            raise ValueError("a recent week has a value exactly when it is not unknown")
        return self


class RecentWeekOutV1(_Api):
    """One of the last 13 weeks, with the stored shipped-below-ordered flag."""

    week_start: date
    targets: dict[Target, RecentTargetOutV1]
    stockout: bool | None

    @model_validator(mode="after")
    def _all_targets(self) -> RecentWeekOutV1:
        _check_targets(self.targets.keys())
        for target, row in self.targets.items():
            if row.value is not None and ("." in row.value.display) != (target in MONEY_TARGETS):
                raise ValueError(f"{target}: display has two places exactly for money targets")
        return self


class DemandForecastDetailV1(_Api):
    """`GET /api/demand-forecast?asin=…`."""

    schema_version: SchemaVersion
    outcome: DetailOutcome
    asin: DisplayAsin
    provenance: ProvenanceV1 | None
    freshness: FreshnessV1
    horizon: HorizonV1 | None
    title: str | None
    title_truncated: bool
    targets: dict[Target, TargetDetailV1] | None
    recent_weeks: Annotated[tuple[RecentWeekOutV1, ...], Field(min_length=13, max_length=13)] | None
    stockout_weeks_last_13: Annotated[int, Field(ge=0, le=13)] | None
    week_state_counts: WeekStateCountsV1 | None

    @model_validator(mode="after")
    def _rules(self) -> DemandForecastDetailV1:
        unavailable = self.outcome == "unavailable"
        if (self.provenance is None) != unavailable or (self.horizon is None) != unavailable:
            raise ValueError("provenance and horizon are None exactly when unavailable")
        if (self.freshness.state == "unavailable") != unavailable:
            raise ValueError("freshness is unavailable exactly when the outcome is")
        item = self.outcome in ("selected", "not_forecast")
        parts = (self.targets, self.recent_weeks, self.stockout_weeks_last_13)
        if any((part is None) == item for part in (*parts, self.week_state_counts)):
            raise ValueError("item fields are set exactly when the product is in the forecast")
        if not item and (self.title is not None or self.title_truncated):
            raise ValueError("an absent product has no title")
        if self.targets is not None:
            _check_targets(self.targets.keys())
            for target, row in self.targets.items():
                bands = row.bands
                figures = () if bands is None else (*bands.p10, *bands.p50, *bands.p90)
                _check_target(
                    target,
                    chosen_model=row.chosen_model,
                    withheld_reason=row.withheld_reason,
                    currency_code=row.currency_code,
                    p50_totals=row.p50_totals,
                    figures=figures,
                )
                if (bands is None) != (row.chosen_model is None):
                    raise ValueError(f"{target}: bands are set exactly when shown")
                if row.chosen_model is None and row.accuracy is not None:
                    raise ValueError(f"{target}: a withheld target has no accuracy")
            shown = any(row.chosen_model is not None for row in self.targets.values())
            if shown != (self.outcome == "selected"):
                raise ValueError("selected exactly when at least one target is shown")
        return self


# --- List -----------------------------------------------------------------------------------

StatusFilter = Literal["shown"] | SkipReason


class ListTargetV1(_Api):
    """One target's stored list-row summary."""

    chosen_model: ModelName | None
    withheld_reason: SkipReason | None
    currency_code: CurrencyCode | None
    p50_totals: dict[ViewKey, FigureV1 | None]


class ListEntryV1(_Api):
    """One stored, ranked list row."""

    rank: Annotated[int, Field(ge=1)]
    asin: DisplayAsin
    title: str | None
    title_truncated: bool
    targets: dict[Target, ListTargetV1]
    stockout_weeks_last_13: Annotated[int, Field(ge=0, le=13)]

    @model_validator(mode="after")
    def _targets(self) -> ListEntryV1:
        _check_targets(self.targets.keys())
        for target, row in self.targets.items():
            _check_target(
                target,
                chosen_model=row.chosen_model,
                withheld_reason=row.withheld_reason,
                currency_code=row.currency_code,
                p50_totals=row.p50_totals,
                figures=(),
            )
        return self


class ListQueryV1(_Api):
    """The request the list answers."""

    status: StatusFilter | None
    page: Annotated[int, Field(ge=1, le=list_pages.MAX_LIST_PAGE)]


class DemandForecastListV1(_Api):
    """`GET /api/demand-forecast[?status=…&page=…]`."""

    schema_version: SchemaVersion
    outcome: ListOutcome
    provenance: ProvenanceV1 | None
    freshness: FreshnessV1
    horizon: HorizonV1 | None
    query: ListQueryV1
    page: Annotated[int, Field(ge=1)]
    page_count: Annotated[int, Field(ge=0)]
    match_count: Annotated[int, Field(ge=0)]
    page_rows: Annotated[int, Field(ge=1)]
    entries: tuple[ListEntryV1, ...]
    item_count: Annotated[int, Field(ge=0)] | None
    counts_by_reason: dict[Target, dict[SkipReason, int]] | None
    counts_by_model: dict[Target, dict[ModelName, int]] | None
    currency_codes: tuple[CurrencyCode, ...]
    status_target: Target

    @model_validator(mode="after")
    def _rules(self) -> DemandForecastListV1:
        unavailable = self.outcome == "unavailable"
        nullable = (
            self.provenance,
            self.horizon,
            self.item_count,
            self.counts_by_reason,
            self.counts_by_model,
        )
        if any((part is None) != unavailable for part in nullable):
            raise ValueError("generation fields are None exactly when unavailable")
        if (self.freshness.state == "unavailable") != unavailable:
            raise ValueError("freshness is unavailable exactly when the outcome is")
        if self.page != self.query.page:
            raise ValueError("page is the requested page")
        if unavailable:
            if self.page_count or self.match_count or self.entries or self.currency_codes:
                raise ValueError("an unavailable list has no pages, matches or entries")
        elif not 1 <= self.page <= self.page_count:
            raise ValueError("page is within page_count")
        if len(self.entries) > self.page_rows:
            raise ValueError("a page holds at most page_rows entries")
        ranks = [entry.rank for entry in self.entries]
        if ranks != sorted(set(ranks)):
            raise ValueError("entries are in rank order, each once")
        if list(self.currency_codes) != sorted(set(self.currency_codes)):
            raise ValueError("currency_codes are sorted, each once")
        return self


# --- Status ---------------------------------------------------------------------------------


class GenerationV1(_Api):
    """The generation `CURRENT` names."""

    generation_id: NonEmpty
    published_at: Instant
    input_end: date


class LastRunV1(_Api):
    """`CURRENT.last_attempt`: the last forecast run's outcome."""

    outcome: LastRunOutcome
    failure_category: str | None
    failure_family: FailureFamily | None
    retry_disposition: Literal["suppress", "retry"]
    transient_retries: Annotated[int, Field(ge=0)]
    at: Instant

    @model_validator(mode="after")
    def _family_with_category(self) -> LastRunV1:
        if (self.failure_category is None) != (self.failure_family is None):
            raise ValueError("failure_category and failure_family are both set or both None")
        return self


class DemandForecastStatusV1(_Api):
    """`GET /api/demand-forecast/status`."""

    schema_version: SchemaVersion
    generation: GenerationV1 | None
    freshness: FreshnessV1
    last_run: LastRunV1 | None
    hold: Hold
    rollback_active: bool
    rolled_back_from: str | None
    next_check_at: Instant
    next_new_input_check_at: Instant

    @model_validator(mode="after")
    def _rules(self) -> DemandForecastStatusV1:
        if (self.generation is None) != (self.freshness.state == "unavailable"):
            raise ValueError("freshness is unavailable exactly when no generation is named")
        if self.rollback_active != (self.rolled_back_from is not None):
            raise ValueError("rolled_back_from is set exactly when a rollback is active")
        held = self.last_run is not None and self.last_run.outcome == "held_quality"
        if (self.hold == "quality") != held:
            raise ValueError("hold is quality exactly when the last run was held")
        return self


class DemandForecastRefusalV1(_Api):
    """Every refusal body: the closed name, its code and the user-visible description."""

    schema_version: SchemaVersion
    refused: ForecastReadRefusal
    code: Annotated[str, StringConstraints(pattern=r"^ADLY-FORECAST_READ-[0-9]{4}$")]
    detail: NonEmpty


API_MODELS: tuple[type[BaseModel], ...] = (
    DemandForecastListV1,
    DemandForecastDetailV1,
    DemandForecastStatusV1,
    DemandForecastRefusalV1,
)
API_ROUTES: tuple[str, ...] = ("GET /api/demand-forecast", "GET /api/demand-forecast/status")
LIST_ROUTE, STATUS_ROUTE = API_ROUTES
API_ENUMS: Mapping[str, tuple[str, ...]] = {
    "SkipReason": tuple(reason.value for reason in SkipReason),
    "ModelName": tuple(model.value for model in ModelName),
    "RunOutcome": get_args(LastRunOutcome),
    "FailureFamily": get_args(FailureFamily),
    "FreshnessCause": get_args(FreshnessCause),
    "AccuracyClass": get_args(AccuracyClass),
    "Hold": get_args(Hold),
    "ForecastReadRefusal": tuple(refusal.value for refusal in ForecastReadRefusal),
}
"""The closed values the page maps to plain words; the frontend tests read them generated."""

# --- Stored views (M1) ----------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationView:
    """A generation's `META` item, parsed and validated by the store adapter."""

    generation_id: str
    input_end: date
    first_forecast_week: date
    item_count: int
    currency_codes: tuple[str, ...]
    counts_by_reason: Mapping[str, int]
    counts_by_model: Mapping[str, int]
    pages: Mapping[str, list_pages.FilterPages]
    first_page: tuple[RosterEntryV1, ...]


@dataclass(frozen=True)
class AsinItemView:
    """One product's `ASIN#` item, parsed: the candidate, its title and its stored list row."""

    candidate: CandidateSeriesV1
    title: str | None
    title_truncated: bool
    roster: RosterEntryV1


# --- Requests -------------------------------------------------------------------------------

_PAGE = re.compile(r"[1-9][0-9]{0,4}")
_CRON = re.compile(r"cron\(([0-9]{1,2}) ([0-9]{1,2}) \* \* \? \*\)")


@dataclass(frozen=True)
class ListRequest:
    """One list request: a closed filter and a page from 1 to `MAX_LIST_PAGE`."""

    filter: str
    page: int

    def __post_init__(self) -> None:
        if self.filter not in list_pages.CLOSED_FILTERS:
            raise ValidationError(STATUS_INVALID, "Choose a listed forecast status.")
        if isinstance(self.page, bool) or not 1 <= self.page <= list_pages.MAX_LIST_PAGE:
            raise ValidationError(PAGE_INVALID, "That page of the forecast doesn't exist.")

    @property
    def status(self) -> StatusFilter | None:
        """The filter as the request named it: None for all, `shown`, or a skip reason."""
        if self.filter == list_pages.ALL:
            return None
        if self.filter == list_pages.SHOWN:
            return "shown"
        return SkipReason(self.filter.removeprefix(list_pages.WITHHELD_PREFIX))


def parse_asin(value: str) -> str:
    """Return a requested ASIN in display form, or refuse it `ASIN_INVALID`."""
    try:
        return parse_request_asin(value)
    except ValueError:
        raise ValidationError(ASIN_INVALID, "Enter a 10-character ASIN.") from None


def list_request(status: str | None, page: str | None) -> ListRequest:
    """Parse the list query's `status` and `page` before any read."""
    if status is None:
        name = list_pages.ALL
    elif status == list_pages.SHOWN:
        name = list_pages.SHOWN
    elif status in SkipReason.__members__.values():
        name = f"{list_pages.WITHHELD_PREFIX}{status}"
    else:
        raise ValidationError(STATUS_INVALID, "Choose a listed forecast status.")
    if page is not None and _PAGE.fullmatch(page) is None:
        raise ValidationError(PAGE_INVALID, "That page of the forecast doesn't exist.")
    return ListRequest(name, 1 if page is None else int(page))


@dataclass(frozen=True)
class DailySchedule:
    """The daily check's UTC time, from `cron(<minute> <hour> * * ? *)`."""

    hour: int
    minute: int

    def __post_init__(self) -> None:
        if not (0 <= self.hour <= 23 and 0 <= self.minute <= 59):  # noqa: PLR2004 -- clock bounds
            raise ValueError(f"{self.hour}:{self.minute} is not a time of day")


def parse_daily_schedule(cron: str) -> DailySchedule:
    """Accept only a daily `cron(<m> <h> * * ? *)` expression."""
    match = _CRON.fullmatch(cron)
    if match is None:
        raise ValueError(f"{cron!r} is not a daily cron(<m> <h> * * ? *) schedule")
    return DailySchedule(hour=int(match.group(2)), minute=int(match.group(1)))


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now is timezone-aware")
    return now.astimezone(UTC)


def next_check_at(now: datetime, schedule: DailySchedule) -> datetime:
    """Return the next daily check strictly after `now`."""
    current = _aware(now)
    today = current.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
    return today if today > current else today + timedelta(days=1)


def next_new_input_check_at(
    now: datetime, schedule: DailySchedule, admission_weekday: str
) -> datetime:
    """Return the next check on `retry.admission_weekday`, the only one that admits new input."""
    weekday = WEEKDAYS.index(admission_weekday)
    check = next_check_at(now, schedule)
    return check + timedelta(days=(weekday - check.weekday()) % 7)


def api_instant(moment: datetime) -> str:
    """Write an aware instant as whole-second UTC with `Z`; sub-seconds are truncated (M3)."""
    return _aware(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def figure(value: str, target: str) -> FigureV1:
    """Return one stored value with its display: two places for money, a whole number for units."""
    if target in MONEY_TARGETS:
        return FigureV1(value=value, display=currency_display_value(value))
    return FigureV1(value=value, display=str(display_whole(Decimal(value))))


def failure_family(category: str) -> FailureFamily:
    """Project a stored failure category onto its plain-words family."""
    if category in HeldCheck.__members__.values():
        return "quality_check"
    if category in HeldCategory.__members__.values():
        return "input_not_ready"
    if category.startswith("job:"):
        return "forecast_job"
    if category.startswith("input_build:"):
        return "input_build"
    if category == RETRY_EXHAUSTED:
        return "retry_exhausted"
    return "other"


@dataclass(frozen=True)
class FreshnessRequest:
    """The pointer, the request's clock and the alarm's threshold."""

    pointer: CurrentPointer
    now: datetime
    stale_after_days: int

    def __post_init__(self) -> None:
        _aware(self.now)


def forecast_freshness(request: FreshnessRequest) -> FreshnessV1:
    """Return fresh, stale with every cause, or unavailable (REQ-6.3)."""
    pointer = request.pointer
    if pointer.published_at is None:
        return FreshnessV1(
            state="unavailable",
            age_days=None,
            stale_after_days=request.stale_after_days,
            causes=(),
        )
    age = current_age_days(pointer.published_at, request.now)
    causes: list[FreshnessCause] = []
    if age > request.stale_after_days:
        causes.append("age_over_limit")
    attempt = pointer.last_attempt
    if attempt is not None and attempt.outcome is RunOutcome.FAILED:
        causes.append("last_run_failed")
    if attempt is not None and attempt.outcome is RunOutcome.HELD_QUALITY:
        causes.append("last_run_held")
    return FreshnessV1(
        state="stale" if causes else "fresh",
        age_days=age,
        stale_after_days=request.stale_after_days,
        causes=tuple(sorted(causes)),
    )


# --- Builders -------------------------------------------------------------------------------


def _incomplete(key: str, detail: str) -> InvariantError:
    return InvariantError(
        FORECAST_STORE_INCOMPLETE,
        f"The forecast store is incomplete at {key}: {detail}.",
        context={"key": key},
    )


def _provenance(pointer: CurrentPointer) -> ProvenanceV1:
    if pointer.generation_id is None or pointer.published_at is None or pointer.input_end is None:
        raise ValueError("provenance needs a pointer that names a generation")
    return ProvenanceV1(
        generation_id=pointer.generation_id,
        published_at=api_instant(pointer.published_at),
        input_end=pointer.input_end,
        first_forecast_week=pointer.input_end + timedelta(days=1),
    )


def _horizon(first_forecast_week: date, serving: ServingPolicy) -> HorizonV1:
    return HorizonV1(
        grain="week",
        week_starts_on="sunday",
        weeks=tuple(first_forecast_week + timedelta(weeks=k) for k in range(13)),
        views=serving.views,
        default_view_weeks=list_pages.RANK_VIEW_WEEKS,
    )


def _totals(target: str, row: RosterTargetV1) -> dict[ViewKey, FigureV1 | None]:
    return {
        view: None if total is None else figure(total, target)
        for view, total in row.p50_totals.items()
    }


def _list_entry(entry: RosterEntryV1) -> ListEntryV1:
    if entry.rank is None:
        raise ValueError(f"{entry.display_asin}: a list entry is ranked")
    return ListEntryV1(
        rank=entry.rank,
        asin=entry.display_asin,
        title=entry.title,
        title_truncated=entry.title_truncated,
        targets={
            target: ListTargetV1(
                chosen_model=row.chosen_model,
                withheld_reason=row.withheld_reason,
                currency_code=row.currency_code,
                p50_totals=_totals(target, row),
            )
            for target, row in entry.targets.items()
        },
        stockout_weeks_last_13=entry.stockout_weeks_last_13,
    )


def _by_target(counts: Mapping[str, int]) -> dict[str, dict[str, int]]:
    """Reshape stored `<target>.<value>` keys by target; every count is the stored one."""
    shaped: dict[str, dict[str, int]] = {target: {} for target in TARGETS}
    for key, count in counts.items():
        target, _, value = key.partition(".")
        if target not in shaped or not value:
            raise ValueError(f"count key {key!r} is not <target>.<value>")
        shaped[target][value] = count
    return shaped


@dataclass(frozen=True)
class ListSource:
    """What one list request read: the pointer, `META` and the page item when one was read."""

    request: ListRequest
    pointer: CurrentPointer
    generation: GenerationView
    page_entries: tuple[RosterEntryV1, ...] | None
    serving: ServingPolicy


def build_unavailable_list(
    request: ListRequest, serving: ServingPolicy, now: datetime
) -> DemandForecastListV1:
    """Build the list when `CURRENT` names no generation (M4)."""
    return DemandForecastListV1(
        schema_version=1,
        outcome="unavailable",
        provenance=None,
        freshness=forecast_freshness(
            FreshnessRequest(CurrentPointer(fence=0), now, serving.stale_after_days)
        ),
        horizon=None,
        query=ListQueryV1(status=request.status, page=request.page),
        page=request.page,
        page_count=0,
        match_count=0,
        page_rows=serving.page_rows,
        entries=(),
        item_count=None,
        counts_by_reason=None,
        counts_by_model=None,
        currency_codes=(),
        status_target=serving.status_target,  # pyright: ignore[reportArgumentType]
    )


def page_entries(source: ListSource) -> tuple[RosterEntryV1, ...]:
    """Return the stored entries of the requested page, checked against `META`."""
    request, generation, rows = source.request, source.generation, source.serving.page_rows
    counts = generation.pages.get(request.filter)
    if counts is None:
        raise _incomplete("META", f"filter {request.filter} is missing from pages")
    if request.page > counts.page_count:
        raise ValidationError(PAGE_INVALID, "That page of the forecast doesn't exist.")
    if counts.entry_count == 0:
        return ()
    if request.filter == list_pages.ALL and request.page == 1:
        entries = generation.first_page
    elif source.page_entries is None:
        raise _incomplete(f"PAGE#{request.filter}#{request.page}", "the page item is missing")
    else:
        entries = source.page_entries
    expected = min(rows, counts.entry_count - (request.page - 1) * rows)
    if len(entries) != expected:
        key = f"PAGE#{request.filter}#{request.page}"
        raise _incomplete(key, f"{len(entries)} entries where META implies {expected}")
    return entries


def build_list(source: ListSource, now: datetime) -> DemandForecastListV1:
    """Build one stored page of the ranked roster, checked against `META` after the join."""
    pointer, generation, request = source.pointer, source.generation, source.request
    if pointer.generation_id != generation.generation_id:
        raise _incomplete("META", "it names another generation than CURRENT")
    entries = page_entries(source)
    counts = generation.pages[request.filter]
    try:
        shaped = tuple(_list_entry(entry) for entry in entries)
        reasons = _by_target(generation.counts_by_reason)
        models = _by_target(generation.counts_by_model)
        return DemandForecastListV1(
            schema_version=1,
            outcome="complete",
            provenance=_provenance(pointer),
            freshness=forecast_freshness(
                FreshnessRequest(pointer, now, source.serving.stale_after_days)
            ),
            horizon=_horizon(generation.first_forecast_week, source.serving),
            query=ListQueryV1(status=request.status, page=request.page),
            page=request.page,
            page_count=counts.page_count,
            match_count=counts.entry_count,
            page_rows=source.serving.page_rows,
            entries=shaped,
            item_count=generation.item_count,
            counts_by_reason=reasons,  # pyright: ignore[reportArgumentType]
            counts_by_model=models,  # pyright: ignore[reportArgumentType]
            currency_codes=tuple(sorted(set(generation.currency_codes))),
            status_target=source.serving.status_target,  # pyright: ignore[reportArgumentType]
        )
    except ValueError as error:
        raise _incomplete("META", type(error).__name__) from error


@dataclass(frozen=True)
class DetailSource:
    """What one detail request read: the pointer and the product's item, or None."""

    asin: str
    pointer: CurrentPointer
    item: AsinItemView | None
    serving: ServingPolicy


def _accuracy(candidate: TargetCandidateV1) -> AccuracyV1 | None:
    label = classify_accuracy(candidate)
    if label is None:
        return None
    score = next(
        e.backtest_score for e in candidate.evaluations if e.model is candidate.chosen_model
    )
    if score is None:
        return None
    return AccuracyV1(
        wql=score.wql,
        mase=score.mase,
        mase_gap=score.mase_gap,
        weeks_in_range=score.weeks_in_range,
        weeks_scored=score.weeks_scored,
        accuracy_class=label,
    )


def _target_detail(target: str, item: AsinItemView) -> TargetDetailV1:
    candidate = item.candidate.targets[target]  # pyright: ignore[reportArgumentType]
    row = item.roster.targets[target]  # pyright: ignore[reportArgumentType]
    bands = candidate.chosen_forecast
    return TargetDetailV1(
        chosen_model=candidate.chosen_model,
        withheld_reason=candidate.withheld_reason,
        currency_code=row.currency_code,
        bands=None
        if bands is None
        else BandsOutV1(
            p10=tuple(figure(v, target) for v in bands.p10),
            p50=tuple(figure(v, target) for v in bands.p50),
            p90=tuple(figure(v, target) for v in bands.p90),
        ),
        p50_totals=_totals(target, row),
        accuracy=_accuracy(candidate),
    )


def _recent(item: AsinItemView) -> tuple[RecentWeekOutV1, ...]:
    return tuple(
        RecentWeekOutV1(
            week_start=week.week_start,
            targets={
                target: RecentTargetOutV1(
                    state=row.state,
                    value=None if row.value is None else figure(row.value, target),
                )
                for target, row in week.targets.items()
            },
            stockout=week.stockout,
        )
        for week in item.candidate.recent_weeks
    )


def build_detail(source: DetailSource, now: datetime) -> DemandForecastDetailV1:
    """Build one product's stored forecast, or the designed absence."""
    pointer, item, serving = source.pointer, source.item, source.serving
    freshness = forecast_freshness(FreshnessRequest(pointer, now, serving.stale_after_days))
    if pointer.generation_id is None or pointer.input_end is None:
        return DemandForecastDetailV1(
            schema_version=1,
            outcome="unavailable",
            asin=source.asin,
            provenance=None,
            freshness=freshness,
            horizon=None,
            title=None,
            title_truncated=False,
            targets=None,
            recent_weeks=None,
            stockout_weeks_last_13=None,
            week_state_counts=None,
        )
    provenance = _provenance(pointer)
    horizon = _horizon(provenance.first_forecast_week, serving)
    if item is None:
        return DemandForecastDetailV1(
            schema_version=1,
            outcome="not_in_forecast",
            asin=source.asin,
            provenance=provenance,
            freshness=freshness,
            horizon=horizon,
            title=None,
            title_truncated=False,
            targets=None,
            recent_weeks=None,
            stockout_weeks_last_13=None,
            week_state_counts=None,
        )
    key = f"ASIN#{source.asin}"
    if item.candidate.display_asin != source.asin or item.roster.display_asin != source.asin:
        raise _incomplete(key, "the item names another ASIN")
    try:
        targets = {target: _target_detail(target, item) for target in TARGETS}
        shown = any(row.chosen_model is not None for row in targets.values())
        return DemandForecastDetailV1(
            schema_version=1,
            outcome="selected" if shown else "not_forecast",
            asin=source.asin,
            provenance=provenance,
            freshness=freshness,
            horizon=horizon,
            title=item.title,
            title_truncated=item.title_truncated,
            targets=targets,  # pyright: ignore[reportArgumentType]
            recent_weeks=_recent(item),
            stockout_weeks_last_13=item.candidate.stockout_weeks_last_13,
            week_state_counts=item.candidate.week_state_counts,
        )
    except ValueError as error:
        raise _incomplete(key, type(error).__name__) from error


@dataclass(frozen=True)
class StatusSource:
    """The pointer and the schedule facts status reads."""

    pointer: CurrentPointer
    serving: ServingPolicy
    schedule: DailySchedule
    admission_weekday: str


def build_status(source: StatusSource, now: datetime) -> DemandForecastStatusV1:
    """Build the generation, freshness, last forecast run, hold, rollback and next checks."""
    pointer = source.pointer
    attempt = pointer.last_attempt
    generation = None
    if pointer.generation_id is not None and pointer.published_at and pointer.input_end:
        generation = GenerationV1(
            generation_id=pointer.generation_id,
            published_at=api_instant(pointer.published_at),
            input_end=pointer.input_end,
        )
    last_run = None
    if attempt is not None:
        category = attempt.failure_category
        last_run = LastRunV1(
            outcome=attempt.outcome.value,  # pyright: ignore[reportArgumentType]
            failure_category=category,
            failure_family=None if category is None else failure_family(category),
            retry_disposition=attempt.retry_disposition.value,  # pyright: ignore[reportArgumentType]
            transient_retries=attempt.transient_retries,
            at=api_instant(attempt.at),
        )
    held = attempt is not None and attempt.outcome is RunOutcome.HELD_QUALITY
    return DemandForecastStatusV1(
        schema_version=1,
        generation=generation,
        freshness=forecast_freshness(
            FreshnessRequest(pointer, now, source.serving.stale_after_days)
        ),
        last_run=last_run,
        hold="quality" if held else "none",
        rollback_active=pointer.rolled_back_from is not None,
        rolled_back_from=pointer.rolled_back_from,
        next_check_at=api_instant(next_check_at(now, source.schedule)),
        next_new_input_check_at=api_instant(
            next_new_input_check_at(now, source.schedule, source.admission_weekday)
        ),
    )


def refusal_body(error: AdlyticsError) -> DemandForecastRefusalV1:
    """Build the refusal body for a coded forecast read error."""
    name = REFUSAL_BY_CODE.get(str(error.code))
    if name is None:
        raise ValueError(f"{error.code} is not a forecast read refusal")
    return DemandForecastRefusalV1(
        schema_version=1,
        refused=name,
        code=str(error.code),
        detail=error.user_visible_description,
    )


def not_published() -> DomainError:
    """Return the tools' refusal when `CURRENT` names no generation."""
    return DomainError(
        FORECAST_NOT_PUBLISHED,
        "No demand forecast has been published yet, so there is nothing to answer from.",
    )
