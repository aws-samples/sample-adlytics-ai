# SPDX-License-Identifier: MIT-0
"""The four demand forecast tools over the shared read service (ADR-163).

Callers may rely on:

- every figure is one the publisher stored, at the server's display scale: two places for money
  (`currency_display_value`), a whole number for units (`display_whole`). Nothing is summed,
  averaged or extended; a stored p50 total is stated only as the tool's own claim line;
- every response carries its grain, units, measurement basis, coverage, freshness, the forecast
  qualifier and a non-additivity rule, so the model never totals weeks or products;
- `forecast_list_asins` returns page 1 of one filter with `match_count` and `page_count`, labelled
  truncated when more match, or one product by exact ASIN. It never pages (agent-contract-tool);
- a withheld product is named in a claim line with its stored reason, worded to say no more than
  the reason establishes and ending with the reason's code (`_Frame.withheld`), so the answer and
  its evaluation read the same words and each next-step line names the products it applies to;
- each withheld reason in a response adds one claim line with the page's next step for it and
  says that the step is a prerequisite, not a promise of a forecast (`NEXT_STEPS`, `NO_PROMISE`);
- arguments are validated here, each failure refused `FORECAST_ARGUMENT_INVALID` with
  `context.rule`, because the tool schema can't state counts or closed values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from adapters.inbound.forecast_read.service import (
    AsinRowsRequest,
    ForecastReadService,
    ListLookupRequest,
    ReadContext,
    ReadResult,
)
from domain.forecasting import list_pages, read_api
from domain.forecasting.boundary import CandidateSeriesV1, RosterEntryV1, RosterTargetV1, Target
from domain.forecasting.models import SkipReason
from domain.forecasting.numeric import display_whole
from domain.forecasting.policy import MONEY_TARGETS, TARGETS, DemandForecastPolicy
from domain.forecasting.publish import CurrentPointer
from domain.forecasting.read_api import AsinItemView, FreshnessV1
from domain.narrative.publication import AnswerQualifier, QualifierKind
from domain.narrative.shaped_response import (
    ClaimField,
    DescriptorField,
    Metadata,
    ResponseData,
    ShapedResponse,
    UnitKind,
    Units,
    non_additivity,
)
from domain.presentation.currency_display import currency_display_text, currency_display_value
from domain.shared_kernel.errors import ValidationError
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import Grain


class ForecastTool(StrEnum):
    """The four forecast tools, by their declared names."""

    LIST = "forecast_list_asins"
    QUERY = "forecast_query_asins"
    SUMMARIZE = "forecast_summarize_asin"
    COMPARE = "forecast_compare_asins"


_ARGUMENTS: Final[Mapping[ForecastTool, frozenset[str]]] = {
    ForecastTool.LIST: frozenset({"asin", "status", "target"}),
    ForecastTool.QUERY: frozenset({"asins", "target", "view_weeks"}),
    ForecastTool.SUMMARIZE: frozenset({"asin", "target", "view_weeks"}),
    ForecastTool.COMPARE: frozenset({"asins", "target", "view_weeks"}),
}
_MIN_ASINS: Final = {ForecastTool.QUERY: 1, ForecastTool.COMPARE: 2}
_NO_SUPPRESSION: Final = Suppression(rows_suppressed=False)
_TARGET_WORDS: Final = {
    "units_ordered": "demand (ordered units)",
    "units_shipped": "shipped units",
    "revenue_shipped": "shipped revenue",
    "cogs_shipped": "shipped cost of goods sold",
}
_CAUSE_WORDS: Final = {
    "age_over_limit": "it is older than the freshness limit",
    "last_run_failed": "the last forecast run failed",
    "last_run_held": "a quality check held the last forecast run",
}
_REASON_WORDS: Final = {
    SkipReason.INSUFFICIENT_CONTEXT: "too little history",
    SkipReason.TARGET_VALUE_MISSING: "missing values in its history",
}
_TRY_AGAIN: Final = "The next weekly update tries again."
_TRY_NEWEST: Final = "The next weekly update tries again with the newest weeks."
_MORE_WEEKS: Final = "A forecast becomes possible as more weeks of sales arrive."
NEXT_STEPS: Final[Mapping[SkipReason, str]] = {
    SkipReason.ASIN_INACTIVE: (
        "If it's still on sale, plan it by hand for now. The next weekly update will check again "
        "after new sales data arrives; a forecast appears only once there's enough complete "
        "history."
    ),
    SkipReason.TARGET_VALUE_MISSING: (
        "The next weekly update tries again. If the gaps stay, ask your administrator to check "
        "this product's Vendor Central sales report."
    ),
    SkipReason.INSUFFICIENT_CONTEXT: (
        "A forecast becomes possible once it has enough complete weeks of sales."
    ),
    SkipReason.HOLDOUT_UNKNOWN: _TRY_NEWEST,
    SkipReason.HOLDOUT_ALL_ZERO: _TRY_NEWEST,
    SkipReason.BACKTEST_BAND_UNORDERED: _TRY_AGAIN,
    SkipReason.SEASONAL_LAG_UNKNOWN: _TRY_AGAIN,
    SkipReason.SEASONAL_LAG_UNKNOWN_AT_ORIGIN: _TRY_AGAIN,
    SkipReason.SEASONAL_RESIDUALS_TOO_FEW: _MORE_WEEKS,
    SkipReason.HISTORY_TOO_SHORT_FOR_SEASON: _MORE_WEEKS,
    SkipReason.ETS_INSUFFICIENT_KNOWN: _TRY_AGAIN,
    SkipReason.ETS_FIT_FAILED: _TRY_AGAIN,
    SkipReason.ACCURACY_NOT_TESTABLE: _TRY_AGAIN,
    SkipReason.FORECAST_NOT_PUBLISHABLE: _TRY_AGAIN,
    SkipReason.FORECAST_SHAPE_INVALID: _TRY_AGAIN,
    SkipReason.FORECAST_NOT_FINITE: _TRY_AGAIN,
    SkipReason.FORECAST_BAND_UNORDERED: _TRY_AGAIN,
    SkipReason.FORECAST_NEGATIVE: _TRY_AGAIN,
    SkipReason.PRODUCT_KEY_INVALID: (
        "Ask your administrator to check the product in Vendor Central."
    ),
    SkipReason.DISPLAY_ASIN_COLLISION: (
        "Ask your administrator to check the products in Vendor Central."
    ),
    SkipReason.CURRENCY_MIXED: (
        "Ask your administrator which marketplace this product should report in."
    ),
}
"""The page's next step for each reason in its undated form, verbatim from `reasonNextStepCopy`
in `frontend/lib/demand-forecast.ts`, which owns the wording. The page shows the dated form once
its status loads; both forms are pinned to `frontend/test/fixtures/demand-forecast-next-steps.json`,
which a test on each side compares, so neither can change alone."""
NO_PROMISE: Final = (
    "A next step says what has to happen first, not that a forecast will follow: a product is "
    "forecast only when a forecasting method passes its checks."
)
"""Chat turned "a forecast appears only once there's enough complete history" into "a forecast
will appear once ..." (dev, Q4); this line keeps the prerequisite a prerequisite."""
FORECAST_RENDERING: Final = (
    "Forecast, not observed: it continues the observed pattern, isn't a causal prediction and "
    "excludes promotions."
)
MIXED_RENDERING: Final = (
    "These figures span forecasts and recent retail history; each recent week's state says "
    "whether its value was observed, inferred as zero or is unknown."
)


@dataclass(frozen=True)
class ForecastToolRequest:
    """One tool call: the tool, the wire arguments, the adapter clock and the correlation id."""

    tool: ForecastTool
    arguments: Mapping[str, object]
    computed_at: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        if self.computed_at.tzinfo is None:
            raise ValueError("computed_at is timezone-aware")


@dataclass(frozen=True)
class _Arguments:
    asins: tuple[str, ...]
    target: Target
    view_weeks: int
    filter: str


def _refuse(rule: str, description: str) -> ValidationError:
    return ValidationError(read_api.FORECAST_ARGUMENT_INVALID, description, context={"rule": rule})


def _asin(value: object) -> str:
    if not isinstance(value, str):
        raise _refuse("asin_format", "An ASIN is 10 letters or digits.")
    try:
        return read_api.parse_asin(value)
    except ValidationError:
        raise _refuse("asin_format", "An ASIN is 10 letters or digits.") from None


def _status(value: object) -> str:
    try:
        return read_api.list_request(value if isinstance(value, str) else "-", None).filter
    except ValidationError:
        raise _refuse(
            "status_unknown", "Use status 'shown' or one of the declared skip reasons."
        ) from None


def _parse(
    tool: ForecastTool, raw: Mapping[str, object], policy: DemandForecastPolicy
) -> _Arguments:
    if set(raw) - _ARGUMENTS[tool]:
        raise _refuse("unknown_argument", f"{tool.value} takes only {sorted(_ARGUMENTS[tool])}.")
    target = raw.get("target", "units_ordered" if tool is ForecastTool.LIST else None)
    if target not in TARGETS:
        raise _refuse("target_unknown", f"target is one of {list(TARGETS)}.")
    view = raw.get("view_weeks", list_pages.RANK_VIEW_WEEKS)
    if isinstance(view, bool) or not isinstance(view, int) or view not in policy.serving.views:
        raise _refuse("view_unknown", f"view_weeks is one of {list(policy.serving.views)}.")
    filter_name = list_pages.ALL
    if "status" in raw:
        if "asin" in raw:
            raise _refuse("asin_with_status", "Give an asin or a status, not both.")
        filter_name = _status(raw["status"])
    if tool in _MIN_ASINS:
        listed = raw.get("asins")
        if not isinstance(listed, list):
            raise _refuse("asins_count", "asins is a list of ASINs.")
        values: list[object] = list(listed)  # pyright: ignore[reportUnknownArgumentType]
        if not _MIN_ASINS[tool] <= len(values) <= policy.serving.compare_max_asins:
            raise _refuse(
                "asins_count",
                f"Give {_MIN_ASINS[tool]} to {policy.serving.compare_max_asins} ASINs.",
            )
        asins = tuple(_asin(value) for value in values)
        if len(set(asins)) != len(asins):
            raise _refuse("asins_unique", "Each ASIN may appear once.")
    elif "asin" in raw:
        asins = (_asin(raw["asin"]),)
    elif tool is ForecastTool.SUMMARIZE:
        raise _refuse("asin_format", "forecast_summarize_asin needs an asin.")
    else:
        asins = ()
    return _Arguments(asins, target, view, filter_name)  # pyright: ignore[reportArgumentType]


def _units(target: str, currency: str | None) -> Units:
    if target in MONEY_TARGETS:
        if currency is None:
            raise ValueError(f"a shown {target} row carries its currency")
        return Units(UnitKind.CURRENCY, currency)
    return Units(UnitKind.COUNT)


def _shown(value: str | None, target: str) -> Decimal | int | None:
    """Return the shown figure for a row: two places for money, a whole number for units."""
    if value is None:
        return None
    if target in MONEY_TARGETS:
        return Decimal(currency_display_value(value))
    return display_whole(Decimal(value))


def _text(value: str, target: str, currency: str | None) -> str:
    if target in MONEY_TARGETS:
        return currency_display_text(value, currency)
    return f"{display_whole(Decimal(value)):,}"


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _end_of(day: date) -> datetime:
    return datetime.combine(day, time.max, tzinfo=UTC)


@dataclass
class _Draft:
    """The response being built: rows, narrative lines and which lines are claims."""

    narrative: list[str]
    claims: list[int]
    values: list[dict[str, str | int | Decimal | None]]
    reasons: list[SkipReason] = field(default_factory=list[SkipReason])

    def claim(self, line: str) -> None:
        self.claims.append(len(self.narrative))
        self.narrative.append(line)

    def name_withheld(self, line: str, reason: SkipReason | None) -> None:
        """Claim the withheld line and note its reason, whose next step is stated once."""
        self.claim(line)
        if reason is not None:
            self.reasons.append(reason)


@dataclass(frozen=True)
class _Call:
    """One validated call: the request, its parsed arguments and the policy."""

    request: ForecastToolRequest
    arguments: _Arguments
    policy: DemandForecastPolicy
    context: ReadContext

    def frame(self, pointer: CurrentPointer) -> _Frame:
        """Bind the pointer the read returned, with its freshness at the request's clock."""
        freshness = read_api.forecast_freshness(
            read_api.FreshnessRequest(
                pointer, self.context.now, self.policy.serving.stale_after_days
            )
        )
        return _Frame(
            self.request,
            pointer,
            self.arguments,
            freshness,
            self.policy.missing_weeks.activity_window_weeks,
        )


@dataclass(frozen=True)
class _Frame:
    """What every response shares: the request, the pointer, the parsed arguments and freshness."""

    request: ForecastToolRequest
    pointer: CurrentPointer
    arguments: _Arguments
    freshness: FreshnessV1
    activity_weeks: int

    @property
    def first_week(self) -> date:
        if self.pointer.input_end is None:
            raise ValueError("a tool answer needs a published generation")
        return self.pointer.input_end + timedelta(days=1)

    def provenance(self) -> str:
        published = self.pointer.published_at
        stamp = "" if published is None else read_api.api_instant(published)
        return f"Forecast from data through {self.pointer.input_end}, published {stamp}."

    def withheld(self, asin: str, target: str, reason: SkipReason | None) -> str:
        """Say why a product isn't forecast, and no more than its stored reason establishes.

        `ASIN_INACTIVE` means only that no sales were reported in the activity window, so the
        line says that and names what it doesn't establish: whether the product is still for sale
        and what its future demand will be. The reason's next step is a separate line, stated
        once per response (`_respond`), so a list of withheld products doesn't repeat it per row.
        """
        if reason is SkipReason.ASIN_INACTIVE:
            why = (
                f"no sales were reported for it in the {self.activity_weeks} weeks of data "
                f"through {self.pointer.input_end}, so there is no recent pattern to forecast. "
                "That alone doesn't say whether it is still for sale or what its future demand "
                "will be"
            )
        else:
            why = "not forecast" if reason is None else _REASON_WORDS.get(reason, "not forecast")
        # The code is the key its next-step line names, so each step is attributable to its
        # products where no value row carries `withheld_reason` (query, compare, money list).
        code = "" if reason is None else f" (withheld as {reason.value})"
        return f"{asin} isn't forecast for {_TARGET_WORDS[target]}: {why}{code}."


def _respond(  # noqa: PLR0913 -- one envelope from its typed parts
    frame: _Frame,
    draft: _Draft,
    *,
    grain: Grain,
    claim_fields: tuple[ClaimField, ...],
    descriptors: tuple[str, ...],
    coverage: tuple[date, date],
    truncated_line: str | None = None,
) -> ShapedResponse:
    for reason in dict.fromkeys(draft.reasons):
        draft.claim(
            f"Next step for a product withheld as {reason.value}: {NEXT_STEPS[reason]} {NO_PROMISE}"
        )
    mixed = grain.dimensions[-1] == "series"
    qualifiers: list[AnswerQualifier] = []
    if draft.values:
        qualifiers.append(
            AnswerQualifier(
                QualifierKind.FORECAST, FORECAST_RENDERING, ("metadata.measurement_basis",)
            )
        )
        if mixed:
            qualifiers.append(
                AnswerQualifier(
                    QualifierKind.MIXED_BASIS, MIXED_RENDERING, ("metadata.measurement_basis",)
                )
            )
    stale = frame.freshness.state == "stale"
    reason = None
    if stale:
        reason = "; ".join(_CAUSE_WORDS[cause] for cause in frame.freshness.causes)
        qualifiers.append(
            AnswerQualifier(
                QualifierKind.STALE,
                f"This forecast is stale: {reason}.",
                ("metadata.freshness_status", "metadata.freshness_reason"),
            )
        )
    if truncated_line is not None:
        qualifiers.append(
            AnswerQualifier(QualifierKind.TRUNCATED, truncated_line, ("metadata.truncated",))
        )
    narrative = [*draft.narrative, *(qualifier.rendering for qualifier in qualifiers)]
    units = claim_fields[0].units if claim_fields else None
    across = tuple(name for name in grain.dimensions if name != "series")
    return ShapedResponse(
        data=ResponseData(
            values=tuple(draft.values),
            units=units,
            grain=grain,
            claim_fields=claim_fields,
            descriptor_fields=tuple(DescriptorField(name) for name in descriptors)
            if draft.values
            else (),
            claim_narrative_indices=tuple(draft.claims),
            additivity=non_additivity(across, "products or forecast weeks"),
        ),
        narrative=tuple(narrative),
        metadata=Metadata(
            facts_read=("demand_forecast",),
            measurement_basis=(MeasurementBasis.MODELLED, MeasurementBasis.RETAIL_ACTUAL)
            if mixed
            else (MeasurementBasis.MODELLED,),
            coverage_start=_midnight(coverage[0]),
            coverage_end=_end_of(coverage[1]),
            freshness_status=FreshnessStatus.STALE if stale else FreshnessStatus.FRESH,
            freshness_reason=reason,
            suppression=_NO_SUPPRESSION,
            row_count=len(draft.values),
            computed_at=frame.request.computed_at,
            correlation_id=frame.request.correlation_id,
            loaded_at=frame.pointer.published_at,
            qualifiers=tuple(qualifiers),
            truncated=truncated_line is not None,
        ),
    )


def _total_fields(target: str, currency: str | None) -> tuple[ClaimField, ...]:
    units = _units(target, currency)
    return (
        ClaimField("p50_total_4w", units, "Forecast total (p50), next 4 weeks"),
        ClaimField("p50_total_8w", units, "Forecast total (p50), next 8 weeks"),
        ClaimField("p50_total_13w", units, "Forecast total (p50), next 13 weeks"),
        ClaimField("stockout_weeks_last_13", Units(UnitKind.COUNT), "Weeks shipped below ordered"),
    )


def _list_row(
    entry: RosterEntryV1, target: str, *, ranked: bool
) -> dict[str, str | int | Decimal | None]:
    row: RosterTargetV1 = entry.targets[target]  # pyright: ignore[reportArgumentType]
    totals = row.p50_totals
    return {
        "asin": entry.display_asin,
        "title": entry.title,
        "model_shown": None if row.chosen_model is None else row.chosen_model.value,
        "withheld_reason": None if row.withheld_reason is None else row.withheld_reason.value,
        "rank": entry.rank if ranked else None,
        "p50_total_4w": _shown(totals["4"], target),
        "p50_total_8w": _shown(totals["8"], target),
        "p50_total_13w": _shown(totals["13"], target),
        "stockout_weeks_last_13": entry.stockout_weeks_last_13,
    }


def _currency(rows: list[RosterTargetV1], target: str) -> str | None:
    if target not in MONEY_TARGETS:
        return None
    codes = sorted({row.currency_code for row in rows if row.chosen_model and row.currency_code})
    return codes[0] if codes else None


def _list(call: _Call, service: ForecastReadService) -> ReadResult[ShapedResponse]:
    arguments = call.arguments
    target = arguments.target
    asin = arguments.asins[0] if arguments.asins else None
    read = service.list_lookup(ListLookupRequest(arguments.filter, target, asin), call.context)
    lookup = read.response
    frame = call.frame(lookup.pointer)
    draft = _Draft([frame.provenance()], [], [])
    truncated = None
    if asin is not None:
        entries = () if lookup.item is None else (lookup.item.roster,)
        if lookup.item is None:
            draft.narrative.append(f"{asin} is not in this forecast.")
    else:
        entries = lookup.entries
        counts = None if lookup.generation is None else lookup.generation.pages[arguments.filter]
        if counts is not None and counts.entry_count > len(entries):
            # A withheld money row is named in the narrative, not shown as a row (DF-S3-OPUS-L1).
            named = sum(
                target in MONEY_TARGETS and entry.targets[target].chosen_model is None  # pyright: ignore[reportAttributeAccessIssue]
                for entry in entries
            )
            split = (
                f", {len(entries) - named} shown and {named} named as not forecast" if named else ""
            )
            truncated = (
                f"This list is truncated: it covers the top {len(entries)} of "
                f"{counts.entry_count:,} matching products (page 1 of {counts.page_count:,})"
                f"{split}; ask about any other product by its ASIN."
            )
        if counts is not None and counts.entry_count == 0:
            draft.narrative.append("No product in this forecast matches that status.")
    currency = _currency([entry.targets[target] for entry in entries], target)  # pyright: ignore[reportArgumentType]
    for entry in entries:
        row: RosterTargetV1 = entry.targets[target]  # pyright: ignore[reportAssignmentType]
        if row.chosen_model is None:
            # The stored reason is a claim line: the answer states it as the tool words it.
            draft.name_withheld(
                frame.withheld(entry.display_asin, target, row.withheld_reason),
                row.withheld_reason,
            )
            if target in MONEY_TARGETS:
                # A withheld money row has no figure and so no currency to state; it is named only.
                continue
        draft.values.append(_list_row(entry, target, ranked=asin is None))
    week = frame.first_week
    answer = _respond(
        frame,
        draft,
        grain=Grain(("asin",)),
        claim_fields=_total_fields(target, currency) if draft.values else (),
        descriptors=("asin", "title", "model_shown", "withheld_reason", "rank"),
        coverage=(week, week + timedelta(weeks=13, days=-1)),
        truncated_line=truncated,
    )
    return ReadResult(answer, read.items_read)


def _band_fields(target: str, currency: str | None, *, recent: bool) -> tuple[ClaimField, ...]:
    units = _units(target, currency)
    fields = (
        ClaimField("p10", units, "Forecast low (p10)"),
        ClaimField("p50", units, "Forecast (p50)"),
        ClaimField("p90", units, "Forecast high (p90)"),
    )
    return (
        (*fields, ClaimField("actual", units, "Recent week value (state in week_state)"))
        if recent
        else fields
    )


def _absent(frame: _Frame, draft: _Draft, found: tuple[str, AsinItemView | None]) -> bool:
    """Name a product that contributes no row, and say whether it does."""
    asin, view = found
    target = frame.arguments.target
    if view is None:
        draft.narrative.append(f"{asin} is not in this forecast.")
        return True
    row = view.candidate.targets[target]  # pyright: ignore[reportArgumentType]
    if row.chosen_model is None:
        draft.name_withheld(frame.withheld(asin, target, row.withheld_reason), row.withheld_reason)
        return True
    return False


def _claims(draft: _Draft, view: AsinItemView, arguments: _Arguments) -> None:
    """State the stored p50 total over the view and the stored backtest score, one line each."""
    target, weeks = arguments.target, arguments.view_weeks
    candidate: CandidateSeriesV1 = view.candidate
    roster = view.roster.targets[target]  # pyright: ignore[reportArgumentType]
    total = roster.p50_totals[str(weeks)]  # pyright: ignore[reportArgumentType]
    asin = candidate.display_asin
    if total is not None:
        draft.claim(
            f"Stored forecast total (p50) of {_TARGET_WORDS[target]} for {asin} over the next "
            f"{weeks} weeks: {_text(total, target, roster.currency_code)}."
        )
    target_row = candidate.targets[target]  # pyright: ignore[reportArgumentType]
    score = next(
        (e.backtest_score for e in target_row.evaluations if e.model is target_row.chosen_model),
        None,
    )
    if score is not None:
        mase = "not measurable" if score.mase is None else score.mase
        draft.claim(
            f"In backtest for {asin}, the actual was inside the range in {score.weeks_in_range} "
            f"of {score.weeks_scored} test weeks; MASE {mase}."
        )


def _rows(call: _Call, service: ForecastReadService) -> ReadResult[ShapedResponse]:
    arguments, tool = call.arguments, call.request.tool
    target, weeks = arguments.target, arguments.view_weeks
    read = service.asin_rows(AsinRowsRequest(arguments.asins, target), call.context)
    rows = read.response
    frame = call.frame(rows.pointer)
    recent = tool is not ForecastTool.QUERY
    draft = _Draft([frame.provenance()], [], [])
    forecast_weeks = [frame.first_week + timedelta(weeks=k) for k in range(weeks)]
    starts: list[date] = []
    currencies: list[RosterTargetV1] = []
    for asin, view in zip(arguments.asins, rows.items, strict=True):
        if _absent(frame, draft, (asin, view)) or view is None:
            continue
        candidate = view.candidate
        currencies.append(view.roster.targets[target])  # pyright: ignore[reportArgumentType]
        model = candidate.targets[target].chosen_model  # pyright: ignore[reportArgumentType]
        bands = candidate.targets[target].chosen_forecast  # pyright: ignore[reportArgumentType]
        if bands is None or model is None:
            continue
        if recent:
            for week in candidate.recent_weeks:
                starts.append(week.week_start)
                stored = week.targets[target]  # pyright: ignore[reportArgumentType]
                draft.values.append(
                    {
                        "asin": asin,
                        "week_start": week.week_start.isoformat(),
                        "series": "recent_actual",
                        "model_shown": None,
                        "week_state": stored.state.value,
                        "stockout": None if week.stockout is None else str(week.stockout).lower(),
                        "measurement_basis": MeasurementBasis.RETAIL_ACTUAL.value,
                        "p10": None,
                        "p50": None,
                        "p90": None,
                        "actual": _shown(stored.value, target),
                    }
                )
        for k, start in enumerate(forecast_weeks):
            row: dict[str, str | int | Decimal | None] = {
                "asin": asin,
                "week_start": start.isoformat(),
                "model_shown": model.value,
                "p10": _shown(bands.p10[k], target),
                "p50": _shown(bands.p50[k], target),
                "p90": _shown(bands.p90[k], target),
            }
            if recent:
                row.update(
                    series="forecast",
                    week_state=None,
                    stockout=None,
                    measurement_basis=MeasurementBasis.MODELLED.value,
                    actual=None,
                )
            draft.values.append(row)
        _claims(draft, view, arguments)
    currency = _currency(currencies, target)
    end = forecast_weeks[-1] + timedelta(days=6)
    start = min(starts) if starts else forecast_weeks[0]
    descriptors = ("asin", "week_start", "model_shown")
    grain = Grain(("asin", "week_start"))
    if recent:
        descriptors = (*descriptors, "series", "week_state", "stockout", "measurement_basis")
        grain = Grain(("asin", "week_start", "series"))
    answer = _respond(
        frame,
        draft,
        grain=grain,
        claim_fields=_band_fields(target, currency, recent=recent) if draft.values else (),
        descriptors=descriptors,
        coverage=(start, end),
    )
    return ReadResult(answer, read.items_read)


def handle(
    request: ForecastToolRequest, service: ForecastReadService, context: ReadContext
) -> ReadResult[ShapedResponse]:
    """Answer one forecast tool call from stored figures only, with its plan's `items_read`."""
    policy = service.policy
    call = _Call(request, _parse(request.tool, request.arguments, policy), policy, context)
    if request.tool is ForecastTool.LIST:
        return _list(call, service)
    return _rows(call, service)
