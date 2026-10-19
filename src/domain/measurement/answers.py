# SPDX-License-Identifier: MIT-0
"""Builds the `ShapedResponse` a measurement question is answered with.

In domain rather than in the tool adapter, for the reason `catalog/answers.py` gives: the
narrative scaffolding is where the honesty lives, and it should be unit-testable without an
Athena account. The adapter is wiring.

Four things this module owns, each of which would otherwise be prose somebody remembers.

**The population defaults, and saying so.** ADR-024 refuses a plan that would sum across a
grain column, and the consequence it records is that the target question is under-specified as
usually phrased: nobody asks for revenue "under the MANUFACTURING distributor view for the
RETAIL selling program". So the tool supplies those, and
**the narrative states every one it supplied** — a default a user cannot see is a population
they did not choose. The declaration and the disclosure are adjacent here on purpose; splitting
them is how a default becomes silent.

**Which defaults are defensible, and which are not.** A *reporting lens* may be defaulted; a
*population* may not. `distributor_view` and `selling_program` are vendor-reporting concepts
describing the same units two ways, so choosing one is choosing a lens. `marketplace_key` is a
different population — defaulting it to the US would answer about a country a European brand
never asked about, and would do it in the wrong currency. It is therefore required, and a
question that omits it is refused by ADR-024's own rule rather than quietly answered.

**The restatement caveat, with a duration.** answerable-questions §5 makes "these figures are
net of returns and will move as returns arrive" mandatory for a recent vendor question. It is
derived from the contract's declared `restatement_window_days` rather than written out, so it
states *how long* and cannot drift from the declaration.

**What the coverage window means, and which day boundary produced it.** A `DateRange` is
deliberately timezone-naive (ADR-004) and `Metadata` needs tz-aware bounds, so the conversion
has to choose a zone. It uses UTC and then says which zone the *source's* business day is in,
because those are different facts and collapsing them would imply the figures are bucketed by
UTC midnight when vendor days close in PST.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal

from domain.catalog.capability import MetricKind
from domain.catalog.freshness import Coverage
from domain.measurement.comparison import (
    CHANGE_ALIAS,
    CHANGE_PERCENT_ALIAS,
    CURRENT_ALIAS,
    PRIOR_ALIAS,
    RANK_ALIAS,
    Comparison,
    Ranking,
    RankOrder,
)
from domain.measurement.figures import Measurement, measure, total_of, varying_columns
from domain.measurement.lenses import LENS_DEFAULTS, defaults_for
from domain.measurement.periodic import GRAIN_SET_COLUMN
from domain.measurement.planning import GRAIN_NAMES, MEASURE_ALIAS, Combination, QueryPlan
from domain.measurement.rosters import (
    PinnedKey,
    RosterOutcome,
    apply_pinned_roster,
    apply_roster,
    coarser_grain_set,
    roster_column,
    roster_lines,
)
from domain.narrative.publication import AnswerQualifier, QualifierKind
from domain.narrative.shaped_response import (
    ClaimField,
    Metadata,
    NonAdditivity,
    ResolutionProvenance,
    ResponseData,
    ShapedResponse,
    UnitKind,
    Units,
    non_additivity,
)
from domain.semantics.compatibility import AggregationKind
from domain.semantics.time import FactTimeClass
from domain.shared_kernel.provenance import FreshnessStatus, Suppression

# `DateRange` is imported at runtime rather than only for annotations, and the difference is not
# cosmetic. A comparison and a ranking each *construct* one to describe the span their figures
# cover; a type-checking-only import satisfies pyright and then raises `NameError` on the first
# call, which is a defect no type checker can see.
from domain.shared_kernel.values import DateRange, Grain

__all__ = [
    "LENS_DEFAULTS",
    "AnswerRequest",
    "ComparisonAnswerRequest",
    "DimensionDisplayLabel",
    "DimensionDisplayLabels",
    "MeasurementContext",
    "RankingAnswerRequest",
    "answer_comparison",
    "answer_measurement",
    "answer_ranking",
    "defaults_for",
    "plan_roster_outcome",
]


class AnswerError(Exception):
    """A measurement result cannot be turned into an honest answer."""


@dataclass(frozen=True, slots=True)
class MeasurementContext:
    """Per-request values every measurement answer carries."""

    computed_at: datetime
    correlation_id: str
    resolution_provenance: tuple[ResolutionProvenance, ...] = ()

    def __post_init__(self) -> None:
        """Validate ahead of `Metadata`, so a failure names the field rather than the envelope."""
        if self.computed_at.tzinfo is None:
            raise AnswerError("computed_at must be timezone-aware.")
        if not self.correlation_id:
            raise AnswerError("correlation_id is required on every answer.")


type Row = dict[str, str | int | Decimal | None]
"""One row of `ResponseData.values`, named so the helpers below read as row work.

The same narrowing `ResponseData` declares: a nested structure in a row is a shape the narration
layer would have to interpret, so a cell is a scalar or nothing."""


@dataclass(frozen=True, slots=True)
class DimensionDisplayLabel:
    """One governed public display text for one canonical dimension value."""

    column: str
    value: str
    label: str

    def __post_init__(self) -> None:
        if not (self.column.strip() and self.value.strip() and self.label.strip()):
            raise AnswerError("a dimension display label requires a column, value, and label.")


@dataclass(frozen=True, slots=True)
class DimensionDisplayLabels:
    """Governed display labels the answer substitutes for canonical dimension values.

    The tool resolves these from governed sources — the media taxonomy, marketplace display
    names, and the entity catalog's public-label projection — before the answer is built, so
    a released row reads "Sponsored Brands" rather than `amazon_sponsored_brands` and a
    campaign row carries its name rather than a protected canonical key.

    Applied strictly *after* `measure` groups rows, never before. Grouping must happen on
    canonical values: two distinct campaigns whose display labels collide (or share one
    placeholder) would otherwise merge into a single fabricated figure.

    An unmapped cell passes through unchanged. Totality for protected identifier columns is
    the tool's obligation — it supplies a placeholder for every value it could not resolve —
    because only the tool knows which columns are protected and what it failed to resolve.
    """

    entries: tuple[DimensionDisplayLabel, ...] = ()

    def __post_init__(self) -> None:
        pairs = tuple((entry.column, entry.value) for entry in self.entries)
        if len(set(pairs)) != len(pairs):
            raise AnswerError("dimension display labels repeat a column and value pair.")

    def project(self, rows: tuple[Row, ...]) -> tuple[Row, ...]:
        """Replace mapped canonical values with display labels, cell by cell."""
        if not self.entries:
            return rows
        index = {(entry.column, entry.value): entry.label for entry in self.entries}
        return tuple(
            {
                name: index.get((name, cell), cell) if isinstance(cell, str) else cell
                for name, cell in row.items()
            }
            for row in rows
        )


def _grain_words(grain: Grain) -> str:
    """Say a grain in customer words: canonical columns lose their `_key` suffix."""
    return ", ".join(name.removesuffix("_key").replace("_", " ") for name in grain.dimensions)


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    """Everything an answer is assembled from.

    A request object rather than parameters because there are six inputs from four places — the
    plan from the domain, the rows and flags from the query port, the coverage from the freshness
    registry, and the context from the caller. Several are tuples of strings, so a positional
    signature would let two be swapped silently.
    """

    plan: QueryPlan
    context: MeasurementContext
    rows: tuple[dict[str, str | int | Decimal | None], ...]
    suppression: Suppression
    coverage: Coverage
    applied_defaults: dict[str, str]
    """The lens columns the tool pinned rather than the caller. Stated in the narrative."""

    additive: bool
    """Whether this metric adds, resolved from the dictionary.

    Required because `Catalog.is_non_additive` always resolves: a default would have to pick a side,
    and either choice would turn an absence into a claim. `False` would tell a reader that a metric
    total for `reach_users`, which is the overstatement this platform exists to refuse.

    Supplied by the tool rather than read from `plan.metric.non_additive`, and that is not a
    convenience. `Catalog.is_non_additive` resolves a ratio through its operands, so `avg_frequency`
    inherits non-additivity from `reach_users` without declaring it. Reading the metric's own flag
    here would return `False` for exactly that case and produce a total for a metric that has none.
    """

    truncated: bool = False

    exact_only_columns: tuple[str, ...] = ()
    """Grain columns the metric's aggregation policy forbids adding across
    (`exact_only_grain_columns`). Rows that differ along one have no total."""

    contract_ids: tuple[str, ...] = ()
    """Which contracts write the facts these figures came from.

    Supplied by the tool because contract identity does not survive planning — a `QueryPlan` names
    facts and columns, and no domain type between here and the query carries a contract id. Sorted
    and deduplicated, which `Metadata` requires rather than assumes.

    Defaulted so a caller that genuinely has none stays constructible, which is what keeps the
    existing tests and the catalog path working. An empty tuple reaching a *user* is the defect
    G-37 records; that is the tool's job to prevent, not this object's to forbid.
    """

    qualified_dq: tuple[str, ...] = ()
    """Typed caveats from current successful-publication evidence, when any qualified."""

    display_labels: DimensionDisplayLabels = DimensionDisplayLabels()
    """Governed display text substituted for canonical dimension values in released rows."""

    period_note: str = ""
    """The server-built sentence naming the complete period a periodic answer reads (ADR-100).

    Built by `resolve_chat_period` from governed publications, so it says when the asked period
    hasn't landed and the answer uses the latest measured one."""


def answer_measurement(request: AnswerRequest) -> ShapedResponse:
    """Turn a query result into the envelope, stating everything that qualifies the figures.

    Zero rows is an answer here, not a refusal — the question was asked of real data and the
    window genuinely held nothing. A refusal means no query ran, and those are raised by
    `plan()` before this is reached.
    """
    plan = request.plan
    measurement = measure(plan, request.rows)
    roster = plan_roster_outcome(plan, measurement)
    if roster is not None and roster.withdrawn:
        # A withdrawn grain set offers none of its figures (design §4.4): every rostered key
        # reads "not available" with the pointer, and no returned row reaches the answer.
        measurement = measure(plan, ())
    values = request.display_labels.project(
        tuple(figure.labelled() for figure in measurement.figures)
    )
    # Computed once and handed to both the envelope and the narrative, never twice. Two call
    # sites could disagree, and the figure a reader sees is the one in the sentence.
    total = _breakdown_total(request, measurement)
    additivity = _additivity(
        request.additive,
        measurement.varying(measurement.grain.dimensions),
        _exact_only_across(request, measurement),
    )
    narrative = _narrative(
        request,
        empty=measurement.is_empty,
        undefined=measurement.undefined_rows,
        measurement=measurement,
        total=total,
        roster=roster,
    )
    qualifiers = _typed_qualifiers(
        _QualifierEvidence(
            plan=plan,
            narrative=narrative,
            coverage=request.coverage,
            suppression=request.suppression,
            requested_windows=(plan.window,),
            truncated=request.truncated,
            qualified_dq=request.qualified_dq,
        )
    )

    return ShapedResponse(
        data=ResponseData(
            values=values,
            units=measurement.units,
            grain=measurement.grain,
            claim_fields=(
                ()
                if measurement.units is None
                else (
                    ClaimField(
                        MEASURE_ALIAS,
                        measurement.units,
                        label=plan.metric.label,
                    ),
                )
            ),
            population_total=total,
            additivity=additivity,
        ),
        narrative=narrative,
        metadata=Metadata(
            facts_read=plan.facts_read,
            measurement_basis=plan.measurement_basis,
            # The window the figures describe, which is the window the query filtered to. Not
            # the loaded window: reporting what is loaded would answer a different question
            # than the one asked. Whether the data reaches across it is carried by
            # `freshness_status` and, when it does not, by a narrative sentence naming what is
            # actually there.
            coverage_start=_start_of(plan.window),
            coverage_end=_end_of(plan.window),
            freshness_status=request.coverage.freshness,
            actual_coverage_start=(
                None if request.coverage.window is None else _start_of(request.coverage.window)
            ),
            actual_coverage_end=(
                None if request.coverage.window is None else _end_of(request.coverage.window)
            ),
            loaded_at=request.coverage.loaded_at,
            freshness_reason=request.coverage.failure_reason,
            time_class=plan.metric.time_class,
            source_timezone=plan.source_timezone,
            qualified_dq=request.qualified_dq,
            suppression=request.suppression,
            row_count=len(values),
            computed_at=request.context.computed_at,
            correlation_id=request.context.correlation_id,
            resolution_provenance=request.context.resolution_provenance,
            truncated=request.truncated,
            semantic_qualifiers=plan.semantic_qualifiers,
            requested_coverage_intervals=(plan.window,),
            actual_coverage_intervals=_actual_coverage_intervals(request.coverage),
            coverage_holes=request.coverage.holes,
            qualifiers=qualifiers,
            contract_ids=request.contract_ids,
        ),
    )


def _requested_windows(current: DateRange, prior: DateRange | None = None) -> tuple[DateRange, ...]:
    """Keep independently requested periods exact and deterministically ordered."""
    return tuple(
        sorted(
            (current,) if prior is None else (prior, current),
            key=lambda item: item.start,
        )
    )


def _actual_coverage_intervals(coverage: Coverage) -> tuple[DateRange, ...]:
    """Expose exact CURRENT intervals, with the legacy window only as a compatibility fallback."""
    return coverage.intervals or (() if coverage.window is None else (coverage.window,))


def _start_of(window: DateRange) -> datetime:
    """Take the window's first instant, in UTC.

    A `DateRange` carries no timezone on purpose (ADR-004): a day means a different instant per
    source, and re-bucketing a pre-aggregated day is fabrication. `Metadata` needs an aware
    datetime, so a zone has to be chosen for the *bound*, and UTC is the only one that is not a
    claim about the data. Which zone the source's business day closes in is a separate fact and
    the narrative states it.
    """
    return datetime.combine(window.start, time.min, tzinfo=UTC)


def _end_of(window: DateRange) -> datetime:
    """Take the window's last instant, in UTC.

    End of day rather than midnight, because `DateRange` is inclusive at both ends — a bound at
    midnight would describe a window one day shorter than the one that was queried.
    """
    return datetime.combine(window.end, time.max, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Qualifiers:
    """Everything that qualifies a set of figures, whichever operation produced them.

    Extracted so the three operations share one ordering of caveats rather than three that drift.
    A breakdown, a period comparison and a ranking differ only in their headline and in the
    sentences specific to what they derived; every disclosure after that is identical, and a
    reader meeting them in a different order for each tool would have to re-learn the answer shape
    per question.
    """

    plan: QueryPlan
    requested_windows: tuple[DateRange, ...]
    """Exact ranges the figures describe. A comparison keeps two periods rather than an envelope."""

    coverage: Coverage
    suppression: Suppression
    applied_defaults: dict[str, str]
    truncated: bool
    empty: bool

    qualified_dq: tuple[str, ...] = ()
    """Qualified successful-publication caveats that must survive answer release."""


ASSOCIATION_QUALIFIER = (
    "This shows association, not cause. Shoppers were not randomly assigned to groups."
)
"""The mandatory qualifier on a `causal: false` metric's answer (ADR-102; design §6.4)."""


def _opening_lines(qualifiers: _Qualifiers, restatement: str | None) -> list[str]:
    """Give the disclosures that come before anything operation-specific.

    `restatement` is passed rather than derived, because a comparison's restatement caveat is a
    different sentence from a breakdown's — it is about the *asymmetry* between two periods rather
    than about one period moving — and both belong in this position.
    """
    plan = qualifiers.plan
    lines: list[str] = []

    if qualifiers.applied_defaults:
        # A default nobody can see is a population the caller did not choose.
        pinned = ", ".join(
            f"{column} = {value}" for column, value in sorted(qualifiers.applied_defaults.items())
        )
        lines.append(
            f"This answer pins {pinned}, which the question did not specify. These are "
            "reporting views of the same units rather than separate populations, so a total "
            "across them would double-count."
        )

    lines.append(
        f"Days are bucketed by the source's own business day in {plan.source_timezone}, not "
        "by UTC. The dates above are that source's days."
    )

    if restatement is not None:
        lines.append(restatement)

    if qualifiers.suppression.rows_suppressed:
        lines.append(
            "Some rows were withheld by Amazon's privacy thresholds, so these figures will "
            f"not reconcile to a total. {qualifiers.suppression.reason}"
        )

    return lines


def _closing_lines(qualifiers: _Qualifiers) -> list[str]:
    """Give the disclosures that come after anything operation-specific."""
    plan = qualifiers.plan
    lines: list[str] = []

    if qualifiers.truncated:
        # The envelope refuses a truncated response whose narrative does not say so, and this
        # is the sentence that satisfies it. It used to carry the consequence for the total as
        # well; the total sentence now owns that, so stating it here too would be one fact in
        # two places and free to disagree with itself.
        lines.append("This result was truncated, so it is a page rather than the whole answer.")

    lines.extend(_coverage_lines(qualifiers))

    if len(set(plan.measurement_basis)) > 1:
        lines.append(
            "These figures span more than one measurement system and are not expected to "
            "reconcile with each other."
        )

    if plan.period is not None:
        # The exact period the plan selected, named with its server label (ADR-100).
        lines.append(
            f"This metric is measured for one complete {plan.period.grain.value}, "
            f"{plan.period.label}, so it must not be read as a daily additive figure."
        )
    elif plan.metric.time_class is not FactTimeClass.DAILY_ADDITIVE:
        lines.append(
            f"This metric is {plan.metric.time_class.value} at {plan.metric.time_grain.value} "
            "grain, so it must not be read as a daily additive figure."
        )

    lines.extend(
        f"Data quality qualified this publication: {caveat}" for caveat in qualifiers.qualified_dq
    )

    if plan.metric.causal is False:
        # ADR-102: mandatory, so evaluation checks it like any other qualifier.
        lines.append(ASSOCIATION_QUALIFIER)

    if plan.metric.notes:
        # Declared in the metric dictionary, so it is carried rather than restated. For
        # `retail_revenue_shipped` this is the net-of-returns caveat answerable-questions §5
        # makes mandatory.
        lines.append(plan.metric.notes.strip())

    return lines


@dataclass(frozen=True, slots=True)
class _QualifierEvidence:
    plan: QueryPlan
    narrative: tuple[str, ...]
    coverage: Coverage
    suppression: Suppression
    requested_windows: tuple[DateRange, ...]
    truncated: bool
    qualified_dq: tuple[str, ...]


def _required_line(narrative: tuple[str, ...], prefix: str) -> str:
    found = tuple(line for line in narrative if line.startswith(prefix))
    if len(found) != 1:
        raise ValueError(f"answer narrative requires exactly one {prefix!r} qualifier.")
    return found[0]


def _typed_qualifiers(evidence: _QualifierEvidence) -> tuple[AnswerQualifier, ...]:
    """Derive mandatory caveat kinds from typed semantics, independently of model prose."""
    qualifiers: list[AnswerQualifier] = []

    def add(kind: QualifierKind, prefix: str, *paths: str) -> None:
        qualifiers.append(
            AnswerQualifier(kind, _required_line(evidence.narrative, prefix), tuple(paths))
        )

    add(
        QualifierKind.SOURCE_TIMEZONE,
        "Days are bucketed by the source's own business day",
        "metadata.source_timezone",
    )
    if evidence.suppression.rows_suppressed:
        add(QualifierKind.SUPPRESSED, "Some rows were withheld", "metadata.suppression")
    if evidence.truncated:
        add(QualifierKind.TRUNCATED, "This result was truncated", "metadata.truncated")
    if (
        evidence.coverage.freshness is not FreshnessStatus.NEVER_LOADED
        and not evidence.coverage.covers_all(evidence.requested_windows)
    ):
        prefix = (
            "Loaded data covers"
            if evidence.coverage.intervals or evidence.coverage.window is not None
            else "The loaded period could not be established"
        )
        add(
            QualifierKind.PARTIAL,
            prefix,
            "metadata.actual_coverage_intervals",
            "metadata.coverage_holes",
        )
    if evidence.coverage.freshness is FreshnessStatus.STALE:
        add(QualifierKind.STALE, "The most recent load finished", "metadata.loaded_at")
    if evidence.coverage.freshness is FreshnessStatus.QUARANTINED:
        add(
            QualifierKind.QUARANTINED,
            "The most recent load was rejected",
            "metadata.freshness_status",
            "metadata.freshness_reason",
        )
    if len(set(evidence.plan.measurement_basis)) > 1:
        add(QualifierKind.MIXED_BASIS, "These figures span", "metadata.measurement_basis")
    if evidence.plan.metric.time_class is not FactTimeClass.DAILY_ADDITIVE:
        add(
            QualifierKind.PERIODIC,
            "This metric is",
            "metadata.time_class",
            "data.grain",
        )
    for caveat in evidence.qualified_dq:
        add(
            QualifierKind.QUALIFIED_DQ,
            f"Data quality qualified this publication: {caveat}",
            "metadata.qualified_dq",
        )
    if evidence.plan.metric.causal is False:
        add(QualifierKind.ASSOCIATION_ONLY, ASSOCIATION_QUALIFIER, "metadata.facts_read")
    return tuple(qualifiers)


def _restatement_line(plan: QueryPlan) -> str | None:
    """State how long this source keeps rewriting its own figures, if it does."""
    if not plan.restatement_window_days:
        return None
    return (
        f"This source rewrites its own figures for up to {plan.restatement_window_days} "
        "days after publication, so a recent period will still move."
    )


def _qualifiers_for(request: AnswerRequest, *, empty: bool) -> _Qualifiers:
    """Read a breakdown's qualifiers off its answer request."""
    return _Qualifiers(
        plan=request.plan,
        requested_windows=(request.plan.window,),
        coverage=request.coverage,
        suppression=request.suppression,
        applied_defaults=request.applied_defaults,
        truncated=request.truncated,
        empty=empty,
        qualified_dq=request.qualified_dq,
    )


def _narrative(  # noqa: PLR0913 — keyword flags all derived from one measurement, not inputs
    request: AnswerRequest,
    *,
    empty: bool,
    measurement: Measurement,
    total: Decimal | int | None,
    undefined: int = 0,
    roster: RosterOutcome | None = None,
) -> tuple[str, ...]:
    """Assemble the factual scaffolding, in a fixed order.

    Short declarative sentences, each stating a consequence rather than a flag. The order is
    stable so a reader meets the figure, then what qualifies it, then what it does not claim.

    `total` is passed rather than recomputed. It is already in the envelope, and the envelope
    refuses a total no sentence states — so a second computation here could only ever disagree
    with the figure the reader is shown. `roster` is the plan's roster applied to the rows the
    query returned (`plan_roster_outcome`), passed for the same reason: a withdrawn grain set's
    figures are already gone from `measurement`.
    """
    plan = request.plan
    window = plan.window
    withdrawn = roster is not None and roster.withdrawn
    qualifiers = _qualifiers_for(request, empty=empty and not withdrawn)
    lines: list[str] = []

    if withdrawn and roster is not None:
        # Said first: the breakdown exists but isn't offered, so the reader isn't left to read
        # an empty answer as no shoppers.
        lines.append(
            f"{plan.metric.label} for {window.days} day(s), {window.start} to {window.end}, "
            f"broken down by {_grain_words(plan.grain)}, isn't offered: {roster.note}."
        )
    elif empty:
        # Said first and plainly. An empty answer read as a zero is the failure this sentence
        # exists to prevent, and it carries no units precisely because there is nothing to
        # denominate.
        lines.append(
            f"No {plan.metric.label.lower()} was recorded between {window.start} and "
            f"{window.end} for this population. That is an absence of data rather than a "
            "figure of zero."
        )
    else:
        lines.append(
            f"{plan.metric.label} for {window.days} day(s), "
            f"{window.start} to {window.end}, broken down by "
            f"{_grain_words(plan.grain)}."
        )

    if request.period_note:
        lines.append(request.period_note)
    if roster is not None:
        lines.extend(roster_lines(roster))
    lines.extend(_opening_lines(qualifiers, _restatement_line(plan)))

    if undefined:
        lines.append(_dropped_rows_line(request, undefined))

    if not empty:
        # Always, when there are rows: either the total or why there is none. The absence is what
        # G-45 is about — a breakdown that simply omits a total looks identical to one that has no
        # total to give, and the reader guaranteed to close that gap by adding the rows himself is
        # the model.
        lines.append(_breakdown_total_line(request, measurement, total))

    lines.extend(_closing_lines(qualifiers))
    return tuple(lines)


def plan_roster_outcome(plan: QueryPlan, measurement: Measurement) -> RosterOutcome | None:
    """Apply a plan's roster to the keys its figures returned, or None for a plan without one.

    Every rostered key with no figure is "not available" (`domain.measurement.rosters`), so chat
    and a bake reading the same answer name the same keys and grade the same loss. A request
    pinning one key is graded on its whole delivery and names that key alone
    (`apply_pinned_roster`), so a withdrawn month reads the same whatever the question's shape.
    """
    roster = plan.roster
    if roster is None:
        return None
    column = roster_column(roster.kind)
    periodic_filters = plan.filters if plan.periodic or plan.join is None else plan.join.filters
    fact = plan.fact if plan.periodic or plan.join is None else plan.join.fact
    grain_set = periodic_filters.get(GRAIN_SET_COLUMN)
    coarser = None if grain_set is None else coarser_grain_set(fact, grain_set)
    pinned = plan.filters.get(column) or (
        None if plan.join is None else plan.join.filters.get(column)
    )
    if pinned is not None:
        return apply_pinned_roster(
            roster, PinnedKey(pinned, answered=bool(measurement.figures)), coarser
        )
    returned = tuple(
        value
        for figure in measurement.figures
        if (value := dict(figure.dimensions).get(column)) is not None
    )
    return apply_roster(roster, returned, coarser)


def _breakdown_total(request: AnswerRequest, measurement: Measurement) -> Decimal | int | None:
    """Give the total a breakdown may state, or `None` when stating one would be dishonest.

    `total_of` answers whether these figures have a total at all — empty, non-additive, or units
    that are not a quantity. Three further reasons are facts about the *read* rather than about the
    figures, and each makes a computable total short by an amount that cannot be stated:

    - **A truncated page.** `LIMIT max_rows + 1` means the rows are a page of an unknown whole, and
      a total over a page is the short figure the truncation note already warns about.
    - **A withholding.** Every measure folds to what could be attributed and what Amazon withheld
      separately (ADR-035), so a figure excludes the withheld amount by construction. Adding the
      withheld side back would not fix it: `withholding_in` totals an amount for the first operand
      only, because a ratio's operands are different quantities in different units, and AMC deletes
      sub-threshold rows rather than flagging them — so what was withheld is a floor, not the gap.
    - **Rows with no figure.** A quotient over a zero denominator, a withheld side, or a measure no
      delivery wrote yields no figure, so a total over the rest does not cover the population.
      `_dropped_rows_line` already says the count is short by that many; this says the total is too.
    """
    if request.truncated or request.suppression.rows_suppressed or measurement.undefined_rows:
        return None
    if _exact_only_across(request, measurement):
        # The rows differ along an axis the metric never adds across: four segments' impressions
        # repeat one impression once per matched segment, so their sum counts no population.
        return None
    return total_of(measurement, additive=request.additive)


def _exact_only_across(request: AnswerRequest, measurement: Measurement) -> tuple[str, ...]:
    """Name the exact-only columns these rows differ along; none for a metric that never adds."""
    return measurement.varying(request.exact_only_columns) if request.additive else ()


def _additivity(
    additive: bool, varying: tuple[str, ...], exact_only: tuple[str, ...]
) -> NonAdditivity | None:
    """Carry the metric's own rule against adding these figures, or None when they add.

    A distinct count or a ratio never adds along any column the rows differ on; a simple sum
    doesn't add along an exact-only column. Rows that don't differ have nothing to add.
    """
    across = exact_only or (() if additive else varying)
    return non_additivity(across, _axis_words(across)) if across else None


def _axis_words(columns: tuple[str, ...]) -> str:
    """Say grain columns by their governed dimension names: `segment_id` is "audience segment"."""
    return ", ".join(GRAIN_NAMES.get(column, column).replace("_", " ") for column in columns)


def _breakdown_total_line(
    request: AnswerRequest, measurement: Measurement, total: Decimal | int | None
) -> str:
    """State the total across every row, or say why there is none to state.

    A breakdown shows every row it read — a page is refused a total, so a stated total covers
    exactly what is displayed. That makes this sentence simpler than the ranking's, which has to
    say what its rows account for out of a population they are a subset of.
    """
    if total is not None:
        return (
            f"Across all {measurement.row_count} row(s) above the total is "
            f"{_denominated(total, measurement.units)}. Every row is shown, so it covers the "
            "whole of what was read."
        )
    reason = _no_total_reason(
        request.plan,
        # The plan's kind rather than the measurement's, because they cannot differ and only one
        # of them is always present. `_units_for` builds `Units(kind=plan.units)`, so a
        # measurement adds the currency *code* and never a different kind — and it is `None`
        # exactly when there are no figures, which is a case this sentence is not reached in.
        # Reading it through a fallback expressed a branch that cannot be taken.
        kind=request.plan.units,
        additive=request.additive,
        truncated=request.truncated,
        suppressed=request.suppression.rows_suppressed,
        undefined=measurement.undefined_rows,
        exact_only=_exact_only_across(request, measurement),
    )
    return _no_total_sentence(reason)


def _dropped_rows_line(request: AnswerRequest, undefined: int) -> str:
    """Say how many rows carry no figure, and name every reason this plan could have produced.

    Dropped rather than reported as zero, because none of the reasons is a zero: a campaign that
    spent nothing did not achieve a ROAS of zero, and a day nobody measured viewability on did not
    have zero viewable impressions. Saying how many is what stops a breakdown from quietly failing
    to add up to its own total.

    **The reasons are enumerated from the plan rather than from the rows, because the count does
    not record which applied to each.** Naming only one told a reader something false about the
    others — attributing a withholding to a zero denominator says the campaign spent nothing when
    Amazon had simply not said (ADR-035), and attributing an unwritten measure to arithmetic says
    the same about a delivery that was never made (ADR-037). Listing what is possible is honest
    about the ambiguity; picking one is not.
    """
    plan = request.plan
    reasons: list[str] = []
    if plan.combination is Combination.QUOTIENT:
        reasons.append("their denominator was zero")
    if request.suppression.rows_suppressed:
        reasons.append("Amazon withheld one side")
    unwritten = tuple(
        measure for measure in plan.measures if measure in plan.partially_written_columns
    )
    if unwritten:
        reasons.append(f"no delivery reported {' or '.join(unwritten)} for them")
    if any(measure in plan.not_returned_columns for measure in plan.measures):
        # A shopper-aggregate cell that didn't come back is stored not_returned (ADR-099 pt 8).
        reasons.append("they're not available: too few shoppers, or none")

    # A figure rather than a ratio: this sentence now also covers a plain measure, and calling a
    # viewable-impressions count a ratio was wrong in the only case that reached it.
    because = _joined(reasons) if reasons else "no figure could be produced for them"
    line = (
        f"{undefined} row(s) are not shown because {because}, which leaves the figure undefined "
        "rather than zero."
    )
    policy = plan.metric.aggregation_policy
    if policy is not None and policy.kind is AggregationKind.EXACT_DISTINCT:
        # Distinct counts are never summed across rows (ADR-099), so no count over the rows
        # above exists to be short.
        return line
    return f"{line} A count over the rows above is therefore short by that many."


def _joined(reasons: list[str]) -> str:
    """Join reasons as alternatives, since the count does not say which applied to which row."""
    if len(reasons) == 1:
        return reasons[0]
    return "either " + ", or ".join(reasons)


_NO_TOTAL = "No total is stated across these rows"
"""The opening of every refusal to total, shared so the two operations refuse in one voice."""


def _no_total_sentence(reason: str) -> str:
    """Compose the refusal, or state it bare when nothing explains it.

    The bare form is not expected to be reachable: every path that produces no total produces a
    reason. It exists because an unexplained absence is the failure this sentence prevents, and
    returning an empty clause after a colon would be worse than saying less.
    """
    return f"{_NO_TOTAL}: {reason}" if reason else f"{_NO_TOTAL}."


def _no_total_reason(  # noqa: PLR0913 — one branch per reason, each read off a different source
    plan: QueryPlan,
    *,
    kind: UnitKind,
    additive: bool,
    truncated: bool,
    suppressed: bool,
    undefined: int,
    exact_only: tuple[str, ...] = (),
) -> str:
    """Name why no total can be stated, taking the first reason that applies.

    **The first rather than all of them, which is the opposite of `_dropped_rows_line`'s choice, and
    the two cases differ in a way worth stating.** There, one count covers rows that were dropped
    for different reasons and the count does not record which, so naming one reason asserts
    something false about the others. Here each reason independently and completely explains why
    there is no total, so one sufficient explanation is honest and five would bury it.

    Ordered by how permanent the reason is. A metric that does not add and units that are not a
    quantity are properties of the question, true of every window it could be asked over; a page, a
    withholding and a row with no figure are properties of this read. A reader who is told the
    permanent reason learns not to ask again, which is the more useful of the two.
    """
    label = plan.metric.label
    denomination = kind.value.replace("_", " ")
    # An ordered tuple rather than a chain of returns, so the precedence this docstring describes is
    # reviewable as data instead of inferred from statement order.
    candidates = (
        # First, because a ratio's units do not say that it is one. `asp_shipped` and
        # `revenue_per_glance_view` are quotients denominated in currency, so reaching the
        # additivity branch instead told a reader that an average selling price counts distinct
        # people — and before that, passed them to `total_of` to be summed. G-46.
        (
            plan.metric.kind is MetricKind.RATIO,
            f"{label} is a ratio, and a sum of ratios is not one.",
        ),
        (
            kind not in (UnitKind.CURRENCY, UnitKind.COUNT),
            f"{label} is a {denomination}, and a sum of {denomination}s is not one.",
        ),
        (
            bool(exact_only),
            f"{label} isn't additive across {_axis_words(exact_only)}, so these rows' figures "
            "are not their sum.",
        ),
        (
            not additive,
            f"{label} counts distinct people, so two rows' figures are not their sum — adding "
            "them would overstate by however much the two populations overlapped.",
        ),
        # Says the consequence and not the mechanism. The truncation disclosure states that the
        # result is a page; repeating it here would read as two separate facts.
        (
            truncated,
            "this result is truncated, so any total over it would be short by an unknown amount.",
        ),
        (
            suppressed,
            "Amazon withheld part of this population, so a total over what remains would be "
            "short by an amount the platform cannot measure.",
        ),
        (
            bool(undefined),
            f"{undefined} row(s) carry no figure at all, so a total over the rest would not "
            "cover the population.",
        ),
    )
    return next((reason for applies, reason in candidates if applies), "")


def _coverage_lines(qualifiers: _Qualifiers) -> tuple[str, ...]:
    """State what the data actually covers, whenever that is not the whole question.

    Silence here would be the most expensive kind: a window half loaded returns figures that
    are individually correct and a total that is short, with nothing looking wrong.

    **Reads `empty` because one of these sentences is about the rows and not only the registry.**
    "The absence above is a gap in the platform" is true of an empty answer and false of one
    carrying figures, and it was emitted for both — an answer reported a real ROAS and, two
    sentences later, that nothing had ever been loaded. ADR-036.

    **And reads the window off the qualifiers rather than the plan, because a comparison has two.**
    Coverage has to be tested against the whole span the figures describe: a comparison whose
    earlier period predates the loaded window returns a prior figure that is short and a change
    that is wrong in the direction nobody checks.
    """
    coverage = qualifiers.coverage
    if coverage.freshness is FreshnessStatus.NEVER_LOADED:
        if qualifiers.empty:
            return (
                "Nothing has ever been loaded for this fact, so the absence above is a gap in "
                "the platform rather than a gap in the business.",
            )
        # Figures did arrive, so there is no absence to explain. What is missing is the record of
        # the load that produced them, which makes completeness unstatable rather than the answer
        # wrong. Not an early return: the sentences below still apply.
        return (
            "No load is on record for this fact, so these figures cannot be checked for "
            "completeness. They are what the tables hold rather than a period known to be "
            "fully loaded.",
        )

    lines: list[str] = []
    if not coverage.covers_all(qualifiers.requested_windows):
        available = coverage.intervals or (() if coverage.window is None else (coverage.window,))
        if available:
            ranges = ", ".join(f"{item.start} to {item.end}" for item in available)
            holes = (
                " Unavailable requested intervals are "
                + ", ".join(f"{item.start} to {item.end}" for item in coverage.holes)
                + "."
                if coverage.holes
                else ""
            )
            lines.append(
                f"Loaded data covers {ranges}, which does not span every period asked about. "
                f"Figures outside those intervals are missing rather than zero.{holes}"
            )
        else:
            lines.append("The loaded period could not be established, so completeness is unknown.")
    if coverage.freshness is FreshnessStatus.STALE and coverage.loaded_at is not None:
        lines.append(
            f"The most recent load finished {coverage.loaded_at:%Y-%m-%d %H:%M} UTC and is "
            "older than this source's staleness threshold, so recent days may be incomplete."
        )
    if coverage.freshness is FreshnessStatus.QUARANTINED:
        lines.append(
            "The most recent load was rejected on provenance, so these figures are the last "
            f"known-good ones rather than current. {coverage.failure_reason}"
        )
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class ComparisonAnswerRequest:
    """Everything a period comparison's answer is assembled from.

    **Carries no `truncated` flag, deliberately.** A breakdown's rows each stand alone, so a
    truncated one is a page of honest figures and a sentence saying so. A comparison pairs rows
    across two result sets, and neither query orders its output — so a truncated pair is two
    arbitrary subsets, and a key present in one page and absent from the other would be reported
    as a period with no figure when it is really a period this answer did not read. The tool
    refuses before reaching here, which is why the flag does not exist to be left false.
    """

    comparison: Comparison
    plan: QueryPlan
    """The current period's plan. Both periods share everything else about it."""

    prior_window: DateRange
    context: MeasurementContext
    coverage: Coverage
    suppression: Suppression
    applied_defaults: dict[str, str]
    additive: bool
    """Whether this metric's rows add (`_Operation.additive`). Required, as on `AnswerRequest`:
    a default would pick a side, and the permissive one leaves a ratio's or a distinct count's
    compared rows unmarked. A comparison states no total, but its rows still carry the rule."""

    exact_only_columns: tuple[str, ...]
    """Grain columns the metric's aggregation policy forbids adding across, as on a breakdown.
    Required for the same reason: an omitted policy must not read as "adds everywhere"."""

    contract_ids: tuple[str, ...] = ()
    qualified_dq: tuple[str, ...] = ()
    display_labels: DimensionDisplayLabels = DimensionDisplayLabels()

    roster_note: tuple[str, ...] = ()
    """The current period's rostered keys with no figure (`plan_roster_outcome`), as sentences."""


def answer_comparison(request: ComparisonAnswerRequest) -> ShapedResponse:
    """Turn two periods' figures into the envelope, stating what the change does not claim.

    Two periods holding nothing between them is an answer here, for the same reason zero rows is
    an answer to a breakdown: the question was asked of real data and both windows genuinely held
    nothing. A refusal means no query ran.
    """
    comparison = request.comparison
    plan = request.plan
    span = DateRange(start=request.prior_window.start, end=plan.window.end)
    values = request.display_labels.project(tuple(row.labelled() for row in comparison.rows))
    keys = tuple(row.dimensions for row in comparison.rows)
    narrative = _comparison_narrative(request)
    qualifiers = _typed_qualifiers(
        _QualifierEvidence(
            plan=plan,
            narrative=narrative,
            coverage=request.coverage,
            suppression=request.suppression,
            requested_windows=_requested_windows(plan.window, request.prior_window),
            truncated=False,
            qualified_dq=request.qualified_dq,
        )
    )

    return ShapedResponse(
        data=ResponseData(
            values=values,
            units=comparison.units,
            grain=comparison.grain,
            claim_fields=(
                ()
                if comparison.units is None
                else (
                    ClaimField(
                        CURRENT_ALIAS,
                        comparison.units,
                        label=f"Current {plan.metric.label}",
                    ),
                    ClaimField(
                        PRIOR_ALIAS,
                        comparison.units,
                        label=f"Previous {plan.metric.label}",
                    ),
                    ClaimField(
                        CHANGE_ALIAS,
                        comparison.units,
                        label=f"Change in {plan.metric.label}",
                    ),
                    ClaimField(
                        CHANGE_PERCENT_ALIAS,
                        Units(UnitKind.PERCENT),
                        label=f"Percentage change in {plan.metric.label}",
                    ),
                )
            ),
            additivity=_additivity(
                request.additive,
                varying_columns(keys, comparison.grain.dimensions),
                varying_columns(keys, request.exact_only_columns) if request.additive else (),
            ),
        ),
        narrative=narrative,
        metadata=Metadata(
            facts_read=plan.facts_read,
            measurement_basis=plan.measurement_basis,
            # The whole span the figures describe, earliest bound to latest, rather than the
            # current period alone. A comparison whose earlier period predates the loaded window
            # is exactly the case coverage exists to catch, and reporting only the recent bound
            # would hide it.
            coverage_start=_start_of(span),
            coverage_end=_end_of(span),
            freshness_status=request.coverage.freshness,
            actual_coverage_start=(
                None if request.coverage.window is None else _start_of(request.coverage.window)
            ),
            actual_coverage_end=(
                None if request.coverage.window is None else _end_of(request.coverage.window)
            ),
            loaded_at=request.coverage.loaded_at,
            freshness_reason=request.coverage.failure_reason,
            time_class=plan.metric.time_class,
            source_timezone=plan.source_timezone,
            qualified_dq=request.qualified_dq,
            suppression=request.suppression,
            row_count=comparison.row_count,
            computed_at=request.context.computed_at,
            correlation_id=request.context.correlation_id,
            resolution_provenance=request.context.resolution_provenance,
            semantic_qualifiers=plan.semantic_qualifiers,
            requested_coverage_intervals=_requested_windows(plan.window, request.prior_window),
            actual_coverage_intervals=_actual_coverage_intervals(request.coverage),
            coverage_holes=request.coverage.holes,
            qualifiers=qualifiers,
            contract_ids=request.contract_ids,
        ),
    )


def _comparison_narrative(
    request: ComparisonAnswerRequest,
) -> tuple[str, ...]:
    """Assemble a comparison's scaffolding, in the shared order with its own caveats inserted."""
    comparison = request.comparison
    plan = request.plan
    current = plan.window
    prior = request.prior_window
    empty = comparison.is_empty

    qualifiers = _Qualifiers(
        plan=plan,
        requested_windows=_requested_windows(current, prior),
        coverage=request.coverage,
        suppression=request.suppression,
        applied_defaults=request.applied_defaults,
        truncated=False,
        empty=empty,
        qualified_dq=request.qualified_dq,
    )

    lines: list[str] = []
    if empty:
        lines.append(
            f"No {plan.metric.label.lower()} was recorded in either period — {current} or "
            f"{prior} — for this population. That is an absence of data rather than a figure "
            "of zero, and there is no change to report between two absences."
        )
    else:
        lines.append(
            f"{plan.metric.label}, {current.days} day(s) {current.start} to {current.end}, "
            f"against {prior.days} day(s) {prior.start} to {prior.end}, broken down by "
            f"{_grain_words(comparison.grain)}."
        )

    lines.extend(request.roster_note)
    lines.extend(
        _opening_lines(qualifiers, _restatement_asymmetry(plan, prior, request.context.computed_at))
    )

    if current.days != prior.days:
        # The single most common way a comparison misleads, and it is invisible in the figures.
        lines.append(
            f"The two periods are different lengths — {current.days} day(s) against "
            f"{prior.days} — so the change reflects that difference as well as any change in "
            "performance."
        )

    if comparison.one_sided:
        lines.append(
            f"{comparison.one_sided} of {comparison.row_count} row(s) appear in only one of the "
            "two periods, so no change is reported for them. A period with no figure is either "
            "one that had no rows or one whose rows Amazon withheld, and those are not the same "
            "answer, so neither is treated as zero."
        )

    without_percent = sum(1 for row in comparison.comparable if row.change_percent is None)
    if without_percent:
        lines.append(
            f"{without_percent} row(s) carry a change with no percentage, because the earlier "
            "period was zero. Growth from nothing has no percentage; the absolute change beside "
            "it is exact."
        )

    lines.extend(_closing_lines(qualifiers))
    return tuple(lines)


def _restatement_asymmetry(
    plan: QueryPlan, prior_window: DateRange, computed_at: datetime
) -> str | None:
    """State the asymmetry a comparison introduces, which a single period does not have.

    **This is the caveat `measurement_compare_windows` earns its own tool on.** Vendor revenue
    restates for forty-five days, so comparing a recent period against an older one compares an
    unsettled figure against a settled one — and the resulting change is partly an artefact of how
    much each period has finished moving. A breakdown's generic "a recent period will still move"
    does not say that, because for one period there is nothing for it to be asymmetric with.

    Derived from the contract's declared `restatement_window_days` rather than written out, so it
    states how long and cannot drift from the declaration.
    """
    days = plan.restatement_window_days
    if not days:
        return None

    today = computed_at.date()
    current_open = (today - plan.window.end).days <= days
    # A prior period always ends before the current one begins, so it cannot be the only unsettled
    # side. Two cases remain, and they need different sentences.
    prior_open = (today - prior_window.end).days <= days

    if current_open and prior_open:
        return (
            f"Both periods are still inside this source's {days}-day restatement window, so both "
            "figures will move and the change between them is provisional."
        )
    if current_open:
        return (
            f"This source rewrites its own figures for up to {days} days after publication. The "
            f"most recent period ended {(today - plan.window.end).days} day(s) ago and is still "
            "inside that window, while the earlier period has settled — so this compares an "
            "unsettled figure against a settled one, and the change will move as restatements "
            "arrive."
        )
    return (
        f"This source rewrites its own figures for up to {days} days after publication, and both "
        "periods are now outside that window, so neither figure is expected to move further."
    )


@dataclass(frozen=True, slots=True)
class RankingAnswerRequest:
    """Everything a ranking's answer is assembled from.

    Carries no `truncated` flag for a sharper version of the reason `ComparisonAnswerRequest`
    gives: a ranking over a truncated result is not a caveated ranking, it is the top n of an
    arbitrary page, and the grand total that makes it honest would be short by an unknown amount.
    """

    ranking: Ranking
    plan: QueryPlan
    context: MeasurementContext
    coverage: Coverage
    suppression: Suppression
    applied_defaults: dict[str, str]
    additive: bool
    """Whether this metric adds, resolved from the dictionary.

    Needed here as well as by `rank`, so the narrative can say *why* a total is absent rather than
    only omitting it. An unexplained absence reads as an oversight and invites the model to supply
    the missing sum itself.
    """

    prior_window: DateRange | None = None
    """The period changes were measured against, present exactly for a ranking by change."""

    exact_only_across: tuple[str, ...] = ()
    """The exact-only grain columns the whole ranked population differs along, found by the
    caller over the population (`Measurement.varying`); `additive` is then False."""

    contract_ids: tuple[str, ...] = ()
    qualified_dq: tuple[str, ...] = ()
    display_labels: DimensionDisplayLabels = DimensionDisplayLabels()

    roster_note: tuple[str, ...] = ()
    """The current period's rostered keys with no figure (`plan_roster_outcome`), as sentences."""


def _ranking_claim_fields(ranking: Ranking, metric_label: str) -> tuple[ClaimField, ...]:
    """Declare each ranking quantity independently; rank is never denominated as the metric."""
    if not ranking.rows or ranking.units is None:
        return ()
    names = set(ranking.rows[0])
    labels = {
        MEASURE_ALIAS: metric_label,
        CURRENT_ALIAS: f"Current {metric_label}",
        PRIOR_ALIAS: f"Previous {metric_label}",
        CHANGE_ALIAS: f"Change in {metric_label}",
    }
    fields: list[ClaimField] = [ClaimField(RANK_ALIAS, Units(UnitKind.ORDINAL), label="Rank")]
    for name in (MEASURE_ALIAS, CURRENT_ALIAS, PRIOR_ALIAS, CHANGE_ALIAS):
        if name in names:
            fields.append(ClaimField(name, ranking.units, label=labels[name]))
    if CHANGE_PERCENT_ALIAS in names:
        fields.append(
            ClaimField(
                CHANGE_PERCENT_ALIAS,
                Units(UnitKind.PERCENT),
                label=f"Percentage change in {metric_label}",
            )
        )
    return tuple(fields)


def answer_ranking(request: RankingAnswerRequest) -> ShapedResponse:
    """Turn a ranking into the envelope, with the total that says what it left out."""
    ranking = request.ranking
    plan = request.plan
    earliest = request.prior_window.start if request.prior_window else plan.window.start
    span = DateRange(start=earliest, end=plan.window.end)
    values = request.display_labels.project(ranking.rows)
    total = _ranking_total(request)
    narrative = _ranking_narrative(request, total=total)
    qualifiers = _typed_qualifiers(
        _QualifierEvidence(
            plan=plan,
            narrative=narrative,
            coverage=request.coverage,
            suppression=request.suppression,
            requested_windows=_requested_windows(plan.window, request.prior_window),
            truncated=False,
            qualified_dq=request.qualified_dq,
        )
    )

    return ShapedResponse(
        data=ResponseData(
            values=values,
            units=ranking.units,
            grain=ranking.grain,
            claim_fields=_ranking_claim_fields(ranking, plan.metric.label),
            population_total=total,
            additivity=_additivity(
                request.additive,
                tuple(
                    column
                    for column in ranking.grain.dimensions
                    if len({row.get(column) for row in ranking.rows}) > 1
                ),
                request.exact_only_across,
            ),
        ),
        narrative=narrative,
        metadata=Metadata(
            facts_read=plan.facts_read,
            measurement_basis=plan.measurement_basis,
            coverage_start=_start_of(span),
            coverage_end=_end_of(span),
            freshness_status=request.coverage.freshness,
            actual_coverage_start=(
                None if request.coverage.window is None else _start_of(request.coverage.window)
            ),
            actual_coverage_end=(
                None if request.coverage.window is None else _end_of(request.coverage.window)
            ),
            loaded_at=request.coverage.loaded_at,
            freshness_reason=request.coverage.failure_reason,
            time_class=plan.metric.time_class,
            source_timezone=plan.source_timezone,
            qualified_dq=request.qualified_dq,
            suppression=request.suppression,
            row_count=len(values),
            computed_at=request.context.computed_at,
            correlation_id=request.context.correlation_id,
            resolution_provenance=request.context.resolution_provenance,
            semantic_qualifiers=plan.semantic_qualifiers,
            requested_coverage_intervals=_requested_windows(plan.window, request.prior_window),
            actual_coverage_intervals=_actual_coverage_intervals(request.coverage),
            coverage_holes=request.coverage.holes,
            qualifiers=qualifiers,
            contract_ids=request.contract_ids,
        ),
    )


def _ranking_total(request: RankingAnswerRequest) -> Decimal | int | None:
    """Give the population total a ranking may state, or `None` when it must not.

    `rank` already refused a total that is not arithmetic — a ratio, a distinct count, an empty
    population. What is refused here is the one case where a computable total would still be a false
    figure: **a withholding.** Every measure folds to what could be attributed and what Amazon
    withheld separately (ADR-035), so `Ranking.total` sums the attributable side across the whole
    population and excludes the withheld part by construction.

    That made the sentence below assert something untrue. It said the total "covers the whole
    population, including them", where "them" is the rows not shown — and under a withholding the
    figure is short by the withheld amount as well, which is the same class of defect as G-45 with
    the platform's name on it rather than the model's. It sat two sentences after the disclosure
    saying these figures "will not reconcile to a total".
    """
    ranking = request.ranking
    if request.suppression.rows_suppressed:
        return None
    return ranking.total


def _ranking_narrative(
    request: RankingAnswerRequest,
    *,
    total: Decimal | int | None = None,
) -> tuple[str, ...]:
    """Assemble a ranking's scaffolding, with the total sentence that quantifies the omission."""
    ranking = request.ranking
    plan = request.plan
    window = plan.window
    prior = request.prior_window
    dimensions = _grain_words(ranking.grain)

    qualifiers = _Qualifiers(
        plan=plan,
        requested_windows=_requested_windows(window, prior),
        coverage=request.coverage,
        suppression=request.suppression,
        applied_defaults=request.applied_defaults,
        truncated=False,
        empty=not ranking.rows,
        qualified_dq=request.qualified_dq,
    )

    lines: list[str] = []
    if not ranking.rows:
        lines.append(_nothing_to_rank_line(request))
    elif ranking.ranked_by is RankOrder.CHANGE and prior is not None:
        lines.append(
            f"{plan.metric.label}: the {len(ranking.rows)} largest increases across "
            f"{ranking.considered} {dimensions} value(s), {window.start} to {window.end} "
            f"against {prior.start} to {prior.end}."
        )
    else:
        lines.append(
            f"{plan.metric.label}: the {len(ranking.rows)} largest of {ranking.considered} "
            f"{dimensions} value(s), for {window.days} day(s) {window.start} to {window.end}."
        )

    # A ranking by change carries the same asymmetry a comparison does, because it is ranking one.
    restatement = (
        _restatement_asymmetry(plan, prior, request.context.computed_at)
        if prior is not None
        else _restatement_line(plan)
    )
    lines.extend(request.roster_note)
    lines.extend(_opening_lines(qualifiers, restatement))

    if ranking.rows:
        lines.append(_total_line(request, total))

    if ranking.not_comparable:
        lines.append(
            f"{ranking.not_comparable} of {ranking.considered} row(s) appear in only one of the "
            "two periods and are excluded from this ranking, because there is no change to rank "
            "them by."
        )

    lines.extend(_closing_lines(qualifiers))
    return tuple(lines)


def _nothing_to_rank_line(request: RankingAnswerRequest) -> str:
    """Say why a ranking is empty, distinguishing no data from nothing comparable."""
    ranking = request.ranking
    window = request.plan.window
    if ranking.considered and ranking.ranked_by is RankOrder.CHANGE:
        return (
            f"No {request.plan.metric.label.lower()} can be ranked by change: every one of the "
            f"{ranking.considered} row(s) found appears in only one of the two periods, so none "
            "has a change to rank by."
        )
    return (
        f"No {request.plan.metric.label.lower()} was recorded between {window.start} and "
        f"{window.end} for this population, so there is nothing to rank. That is an absence of "
        "data rather than a figure of zero."
    )


def _total_line(request: RankingAnswerRequest, total: Decimal | int | None) -> str:
    """State what the ranked rows account for out of the whole population.

    **Both ends of the comparison are pre-computed, and that is the point of the sentence.** A
    grand total alone only answers "so is that most of my spend" if somebody adds the rows above
    it up, and the reader guaranteed to be doing that is the model — which never sums, divides or
    infers a figure. So the subtotal and the total both arrive already added.

    `total` is the figure the envelope carries, decided by `_ranking_total` and passed in rather
    than read off the ranking again, so the sentence and the field cannot state different numbers.
    """
    ranking = request.ranking
    if total is None or ranking.shown is None:
        return _no_total_line(request)

    return (
        f"The {len(ranking.rows)} row(s) above account for "
        f"{_denominated(ranking.shown, ranking.units)} of "
        f"{_denominated(total, ranking.units)} across all {ranking.considered}, so "
        f"{ranking.excluded} row(s) are not shown. The total covers the whole population, "
        "including them."
    )


def _no_total_line(request: RankingAnswerRequest) -> str:
    """Say why no total is stated, so the absence does not read as an oversight."""
    ranking = request.ranking
    reason = _no_total_reason(
        request.plan,
        # As on the breakdown path: reached only with rows present, so the ranking's own units are
        # never absent and their kind is the plan's by construction.
        kind=request.plan.units,
        additive=request.additive,
        # A ranking is never truncated: the tool refuses a truncated read before reaching here,
        # which is why `RankingAnswerRequest` carries no flag to pass.
        truncated=False,
        suppressed=request.suppression.rows_suppressed,
        # A ranking's rows are figures by construction, so there is no undefined row to count.
        undefined=0,
        exact_only=request.exact_only_across,
    )
    tail = f"{ranking.excluded} of {ranking.considered} row(s) are not shown."
    return f"{_no_total_sentence(reason)} {tail}"


def _denominated(value: Decimal | int, units: Units | None) -> str:
    """Label a total with its currency, or leave a count bare.

    A monetary figure and its currency are never separable — the canonical model forbids a bare
    amount — and this sentence is prose, so the code goes in the string rather than in a field
    beside it. The figure is interpolated unformatted, which the envelope's own invariant depends
    on: it looks for `str(total)` in the narrative, so a total reformatted here would be a total
    no sentence states.
    """
    if units is not None and units.kind is UnitKind.CURRENCY and units.currency_code:
        return f"{value} {units.currency_code}"
    return str(value)
