# SPDX-License-Identifier: MIT-0
"""Turn a question into a validated plan, or refuse it with a reason.

The half of querying that is a decision rather than I/O. This module answers *what* should
be read — which fact, which measure, grouped by which columns, over which window — and the
Athena adapter answers how to say it in SQL. `docs/repo-structure.md` puts query composition
in `adapters/outbound/athena`, so no SQL text appears here, and none should: a plan is
engine-independent and the allowlist it enforces is not.

**No metric is defined here.** Definitions are read from `contracts/shared/metric-dictionary.yaml`
through `domain/catalog`, per the hexagonal steering. What this module owns is the arithmetic
of aggregation: which columns may be summed away and which may not.

That is the rule worth stating plainly, because it is the one this module exists for.

**A fact's grain declares what makes one row unique. Aggregating away a grain column is a
claim that summing across it is addition — and for some columns it is not.** Summing
`fct_retail_sales` across `marketplace_key` adds distinct populations, which is arithmetic.
Summing it across `distributor_view` adds a MANUFACTURING pull to a SOURCING pull, which
double-counts the same units under two lenses — the contract says so in as many words, and
[canonical-model](../../../docs/canonical-model.md) puts both option columns in the grain
"because they change which population the row describes".

Nothing in the contract distinguishes the two cases. `from_extraction_option` does not: it
marks columns AMC *synthesises* from workflow options, and the vendor option columns have
real source paths. So rather than guess which grain columns are safe to sum over, a plan
must **account for every one of them** — grouped, or filtered, or the plan is refused naming
the ones left over. A caller that genuinely wants to sum across an option set says so by
filtering it, which is a decision recorded in the plan rather than an accident in the SQL.

The same rule removes a currency hazard for free. `marketplace_key` must be accounted for,
so a monetary figure can never silently span two marketplaces and therefore two currencies —
which `Money` would refuse to add anyway, but by then the SQL has already summed them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Self

from domain.catalog.capability import (
    PERIOD_GRAIN_SELECTOR,
    Catalog,
    CatalogError,
    CrossDeliveryException,
    DeliveryRatio,
    Metric,
    MetricCompatibilityMode,
    MetricKind,
    Refusal,
    RefusalReason,
    references_in_definition,
)
from domain.data_contracts.contract import DataContract, NullMeaning
from domain.data_contracts.schema import CONFORMED_PROVENANCE_COLUMNS, CanonicalType
from domain.measurement.pack_coverage import (
    PackCoverageRequest,
    PackDayVersions,
    PackVersionedColumn,
    pack_versioned_columns,
    require_pack_coverage,
)
from domain.measurement.periodic import (
    GRAIN_SET_COLUMN,
    PERIOD_END_COLUMN,
    PERIOD_START_COLUMN,
    ROW_KIND_COLUMN,
    PeriodicPlanning,
    periodic_leaves,
    require_exact_period,
)
from domain.measurement.rosters import (
    PERIODIC_CELL_COLUMNS,
    Roster,
    RosterKind,
    require_roster,
    roster_column,
    roster_kind_for,
)
from domain.narrative.shaped_response import UnitKind
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.compatibility import (
    NETWORK_AXES,
    AggregationOperation,
    AggregationPolicy,
    AxisBehavior,
    CompatibilityDifference,
    CompatibilityField,
    CompatibilitySignature,
    CompositionOperation,
    CompositionRule,
    RatioOfAggregatesPolicy,
    SemanticAxis,
    SemanticCompatibilityError,
    validate_composition,
)
from domain.semantics.identity import PlatformKey
from domain.semantics.time import FactTimeClass, TimeGrain
from domain.shared_kernel.errors import ErrorCode, ValidationError
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shared_kernel.values import DateRange, Grain

__all__ = [
    "DATE_COLUMN",
    "GRAIN_COLUMNS",
    "GRAIN_NAMES",
    "Combination",
    "FactShape",
    "FactShapes",
    "GrainSetShape",
    "JoinSide",
    "PlanRequest",
    "QueryPlan",
    "exact_only_grain_columns",
    "leaf_references",
    "measure_alias",
    "plan",
    "served_grain_set",
    "withheld_alias",
    "withheld_rows_alias",
]

DATE_COLUMN = "date_key"
"""The column every window filter applies to. In the grain of every fact."""

UNKNOWN_DIMENSION = ErrorCode(area="PLAN", number=2)
"""A question named a grain this model has no dimension for, or named none at all.

Classified rather than a bare `ValueError` so the refusal reaches the caller naming what would have
worked. `PlanRequest.__post_init__` records what happened when it did not.
"""

UNFILTERABLE_COLUMN = ErrorCode(area="PLAN", number=1)
"""A filter named something that is not a column of the fact.

ValidationError rather than DomainError: the request is outside its contract, the same
class as an unknown metric name. It does not alarm (ADR-010).
"""

MEASURE_ALIAS = "value"
"""What the aggregated measure is called in a result row.

Declared here because **both** halves of the read path need to agree about it and they did not.
An adapter composes `SUM(revenue_shipped) AS value`, so a row comes back keyed `value`; `measure()`
was reading it back under the *contract's* column name, `revenue_shipped`. Each half was
self-consistent and tested, and the seam between them was covered by nothing — the composition
tests assert SQL text, the query integration tests read rows directly, and the figures tests built
their own rows under the column name. The first call that put a real result through `measure()`
failed with "a result row carries no value for the measure", about a column the query never
returned.

The alias exists at all because the answer's own vocabulary should not depend on which column a
metric happens to aggregate: `Figure.labelled()` emits `value`, so a caller reading rows out does
not have to know that `retail_revenue_shipped` comes from `revenue_shipped`.
"""


def measure_alias(index: int) -> str:
    """Name the nth aggregate in a composed statement.

    Numbered because a plan may carry more than one. A same-fact ratio aggregates two columns
    and divides them; a difference-of-sums aggregates two and subtracts. Both are one query over
    one fact, so both come back in one row and each aggregate needs its own name.

    Distinct from `MEASURE_ALIAS`, and the split is the point: these are the *inputs* a row
    carries, and `MEASURE_ALIAS` is the single combined *output* `Figure.labelled()` emits. A
    caller reading an answer never sees an operand, so adding a plan kind does not change the
    response shape.
    """
    return f"{MEASURE_ALIAS}_{index}"


def _folded_columns(suppression_columns: tuple[str, ...]) -> tuple[str, ...]:
    """Name the grain columns a query accounts for by folding rather than grouping or filtering.

    Only the suppression discriminator, and only where the fact declares one.
    [ADR-024](../../../docs/decisions/adr-024-plans-account-for-every-grain-column.md) refuses a
    plan that would sum across a grain column, because summing across one is a claim that it is
    addition. Splitting a measure into an attributable half and a withheld half is the opposite
    claim — it says the column distinguishes two populations and reports each — so it accounts for
    the discriminator more completely than a filter did.

    **This is what stops every AMC question having to arrive carrying
    `filters={"rows_suppressed": "false"}`.** That filter selected one population and silently
    discarded the other, which is why `LENS_DEFAULTS` was right to refuse to supply it by default.
    ADR-035 removes the need rather than defaulting it.
    """
    return (suppression_columns[0],) if suppression_columns else ()


def withheld_alias(index: int) -> str:
    """Name how much the nth side's measure had withheld for this group.

    A quantity rather than a flag, which is ADR-035's whole decision. A boolean can say a group is
    incomplete; only an amount lets an answer say *"a further 457 impressions came from locations
    Amazon withheld"*, which is the sentence
    [provenance-chain](../../../docs/concepts/provenance-chain.md) §3.2 requires.
    """
    return f"withheld_{index}"


def withheld_rows_alias(index: int) -> str:
    """Name how many of the nth side's rows were withheld for this group.

    Needed alongside the quantity, not instead of it. **A withheld measure can legitimately sum to
    zero** — a suppressed row with no clicks contributes nothing to the amount — and without a count
    that is indistinguishable from nothing having been withheld. It is also what decides whether an
    absent side of a join is a provable zero or a fabrication, the question `_side_value` turns on.
    """
    return f"withheld_rows_{index}"


class Combination(StrEnum):
    """How a plan's aggregates become the one figure a row reports.

    The kinds are distinguished because each is a different claim about arithmetic, and the
    dictionary already states which applies — a simple metric declares an expression, a ratio
    declares two operands. Nothing here is inferred from the shape of the data.
    """

    SINGLE = "single"
    """One aggregate, reported as it came back. Every plain additive measure."""

    DIFFERENCE = "difference"
    """Two aggregates, subtracted. `attributed_revenue_halo_14d` is the whole population:
    Amazon publishes the total and the advertised-only portion, and the halo is what is left."""

    QUOTIENT = "quotient"
    """Two aggregates over one fact, divided. Every ratio whose operands share a fact.

    Divided *after* aggregating, never averaged from per-row quotients — the mean of ratios is
    not the ratio of sums, and the difference is largest exactly where the denominators are
    most uneven.
    """


CURRENCY_COLUMN = "currency_code"
"""The column that says what a monetary figure is denominated in.

One per fact, uniformly, which is [ADR-021](../../../docs/decisions/adr-021-money-column-shape.md)'s
decision. Present on the five facts that carry money and in the grain of none of them — so a
plan has to ask for it deliberately, and a plan that did not would leave the tool labelling
an amount by assuming which currency a marketplace uses.
"""

GRAIN_COLUMNS: Mapping[str, str] = {
    "date": DATE_COLUMN,
    "ecosystem": "ecosystem_key",
    "platform": "platform_key",
    "channel_group": "channel_group_key",
    "product": "amazon_asin",
    "marketplace": "marketplace_key",
    "channel": "channel_key",
    "campaign": "campaign_key",
    "ad_product": "ad_product_key",
    "exposure_group": "exposure_group",
    "frequency_bucket": "frequency_bucket",
    # A DSP behaviour segment within its namespace; fct_segment_delivery carries one namespace.
    "audience_segment": "segment_id",
    "identity_space": "identity_space",
    # Cohorts (ADR-104): the acquisition channel group, the cohort's first month and follow-up k.
    "acquisition_channel_group": "acquisition_channel_group",
    "cohort": "cohort_month",
    "months_since_acquisition": "months_since_acquisition",
}
"""The dimension names the metric dictionary uses, mapped to canonical columns.

Declared rather than derived by appending `_key`, because that rule is wrong exactly once
and silently: `product` resolves to `amazon_asin`, not `product_key`. ADR-023 removed the
surrogate after establishing that every fact had been mapping an ASIN straight through, so a
mechanical mapping would generate a column no table has.

A test checks every value here exists on the facts that declare the dimension, so the map
cannot drift from the generated tables.
"""


_GRAIN_AXES: Mapping[str, SemanticAxis] = {
    "date": SemanticAxis.TIME,
    "ecosystem": SemanticAxis.ECOSYSTEM,
    "platform": SemanticAxis.PLATFORM,
    "channel_group": SemanticAxis.CHANNEL_GROUP,
    "product": SemanticAxis.PRODUCT,
    "marketplace": SemanticAxis.MARKETPLACE,
    "channel": SemanticAxis.CHANNEL,
    "campaign": SemanticAxis.CAMPAIGN,
    "ad_product": SemanticAxis.AD_PRODUCT,
    "exposure_group": SemanticAxis.EXPOSURE_GROUP,
    "frequency_bucket": SemanticAxis.FREQUENCY_BUCKET,
    "audience_segment": SemanticAxis.AUDIENCE_SEGMENT,
    "identity_space": SemanticAxis.IDENTITY_SPACE,
    "attribution_model": SemanticAxis.ATTRIBUTION_MODEL,
    "acquisition_channel_group": SemanticAxis.ACQUISITION_CHANNEL_GROUP,
    "cohort": SemanticAxis.COHORT,
    "months_since_acquisition": SemanticAxis.MONTHS_SINCE_ACQUISITION,
}


GRAIN_NAMES: Mapping[str, str] = {column: name for name, column in GRAIN_COLUMNS.items()}
"""Each canonical grain column's governed dimension name: the words a customer reads for it."""


def exact_only_grain_columns(metric: Metric, grains: Sequence[str]) -> tuple[str, ...]:
    """Name the grain columns a simple metric's figures must not be added across.

    Read off the metric's aggregation policy, so the dictionary decides: `segment_impressions`
    holds `audience_segment` exact_only because one impression repeats once per matched segment
    (D-4). A composite has no policy of its own; a ratio's rows never add at all.
    """
    policy = metric.aggregation_policy
    if policy is None:
        return ()
    return tuple(
        GRAIN_COLUMNS[name]
        for name in grains
        if (axis := _GRAIN_AXES.get(name)) is not None
        and axis in policy.axes
        and not policy.axes[axis].aggregation.permits(AggregationOperation.SUM)
    )


_SOURCE_OPTION_COLUMNS = frozenset({"distributor_view", "selling_program", "report_options_hash"})
_COLUMN_AXES: Mapping[str, SemanticAxis] = {
    **{column: _GRAIN_AXES[name] for name, column in GRAIN_COLUMNS.items()},
    "attribution_model_key": SemanticAxis.ATTRIBUTION_MODEL,
    **dict.fromkeys(_SOURCE_OPTION_COLUMNS, SemanticAxis.SOURCE_OPTION),
}


@dataclass(frozen=True, slots=True)
class GrainSetShape:
    """One served grain set of a shopper-aggregate fact: the grain its own contract declares."""

    name: str
    grain_columns: tuple[str, ...]
    not_applicable: tuple[str, ...] = ()
    """Columns this grain set stores NULL by option, so a request can't group by them here."""
    roster: RosterKind | None = None
    """The roster this grain set's cells are graded against (`domain.measurement.rosters`)."""
    period_grains: tuple[TimeGrain, ...] = ()
    """The periods its contract delivers (`time_grain`'s value domain): month only on pack 3."""
    absent_by_row_kind: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Per `row_kind` value, the grain columns its rows never carry (NULL by option): a C2
    delivery's `profiled_total` row has no `segment_id` (packs 10-12, design §5.5). A figure
    pinned to that row kind needn't group by them, because its rows hold one NULL there."""

    def roster_for(
        self, group_by: tuple[str, ...], filters: Mapping[str, str]
    ) -> RosterKind | None:
        """Return the roster a figure here is graded against, or None.

        A segment roster grades segment rows only: a C2 figure read from the delivery's
        `profiled_total` row (packs 10-12) neither groups nor pins a segment, so nothing is
        graded. Every other roster applies to every figure of its grain set, as before.
        """
        if self.roster is RosterKind.SEGMENT:
            column = roster_column(RosterKind.SEGMENT)
            if column not in group_by and column not in filters:
                return None
        return self.roster

    def absent_on(self, filters: Mapping[str, str]) -> tuple[str, ...]:
        """Return the grain columns the pinned row kind's rows never carry, or nothing."""
        kind = filters.get(ROW_KIND_COLUMN)
        return next((columns for value, columns in self.absent_by_row_kind if value == kind), ())

    @classmethod
    def of(cls, contract: DataContract) -> tuple[Self, ...]:
        """Describe a contract's grain set, or nothing when it stamps no `grain_set` literal."""
        mapping = contract.field_for(GRAIN_SET_COLUMN)
        if (
            mapping is None
            or mapping.literal_value is None
            or GRAIN_SET_COLUMN not in contract.grain
        ):
            return ()
        not_applicable = tuple(
            item.target_column
            for item in contract.fields
            if item.nullable and item.null_meaning is NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS
        )
        return (
            cls(
                name=str(mapping.literal_value),
                grain_columns=contract.grain,
                not_applicable=not_applicable,
                roster=roster_kind_for(contract),
                period_grains=_declared_period_grains(contract),
                absent_by_row_kind=_absent_by_row_kind(contract, not_applicable),
            ),
        )

    @property
    def split_columns(self) -> frozenset[str]:
        """The grain columns finer than the period and marketplace every grain set shares."""
        return frozenset(self.grain_columns) - PERIODIC_CELL_COLUMNS


def _declared_period_grains(contract: DataContract) -> tuple[TimeGrain, ...]:
    """Return the period grains a grain-set contract's `time_grain` field declares, in order."""
    mapping = contract.field_for(PERIOD_GRAIN_SELECTOR)
    declared = () if mapping is None else mapping.value_domain
    return tuple(grain for grain in TimeGrain if grain.value in declared)


def served_grain_set(grain_sets: Sequence[GrainSetShape], requested: frozenset[str]) -> str | None:
    """Choose the grain set a request reads: the finest whose own split it names (D-1).

    A request naming only the marketplace reads the platform grain set; one naming the ad product
    reads the channel grain set, and one naming the campaign the campaign grain set. A figure is
    never derived from another grain set, so a request naming a column no grain set splits by is
    left to the planner's refusal. Of two that fit equally, the one storing none of the requested
    columns empty is read; None when the fact has no grain sets, or when that leaves no one.

    A cell column every grain set of the fact shares splits none from another, so it doesn't
    choose one: packs 6 and 7 both key on `frequency_bucket`, `identity_space`, `bucket_scheme`
    and `row_kind`, and only pack 7's `ad_product_key` splits them.
    """
    shared: frozenset[str] = frozenset()
    if len(grain_sets) > 1:
        shared = grain_sets[0].split_columns
        for item in grain_sets[1:]:
            shared &= item.split_columns

    def split(item: GrainSetShape) -> frozenset[str]:
        return item.split_columns - shared

    fitting = sorted(
        (item for item in grain_sets if split(item) <= requested),
        key=lambda item: len(split(item)),
        reverse=True,
    )
    if not fitting:
        return None
    finest = tuple(item for item in fitting if len(split(item)) == len(split(fitting[0])))
    if len(finest) > 1:
        # A campaign and an ad product together: the channel grain set stores the campaign
        # empty, and the campaign grain set carries the ad product that follows from it.
        finest = tuple(item for item in finest if not requested.intersection(item.not_applicable))
    return finest[0].name if len(finest) == 1 else None


def _absent_by_row_kind(
    contract: DataContract, not_applicable: tuple[str, ...]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Map each one-value `row_kind` cell set to the NULL-by-option grain columns it doesn't key."""
    cells = contract.expected_cells
    if cells is None or ROW_KIND_COLUMN not in contract.grain:
        return ()
    found: list[tuple[str, tuple[str, ...]]] = []
    for cell_set in cells.sets:
        axis = next((item for item in cell_set.cells if item.column == ROW_KIND_COLUMN), None)
        if axis is None or len(axis.values) != 1:
            continue
        absent = tuple(
            column
            for column in contract.grain
            if column in not_applicable and column not in cell_set.columns
        )
        if absent:
            found.append((axis.values[0], absent))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class FactShape:
    """What a plan needs to know about the fact it reads.

    Supplied by the caller rather than looked up, because loading contracts is a filesystem
    concern and this module is pure. Build it with `FactShape.of` so the column list is
    complete; constructing one by hand risks omitting the conformed provenance columns, which
    no field mapping declares.

    Both tuples are required and must be non-empty, for different reasons. `grain_columns` is
    the input to the aggregation rule, so an empty one turns that check into a no-op. `columns`
    is what makes a filter key safe to put in SQL — see `plan`.
    """

    name: str
    grain_columns: tuple[str, ...]
    columns: tuple[str, ...]

    source_timezone: str = ""
    """The zone the source's business day closes in. Vendor is PST; AMC is UTC.

    Carried because an answer has to say which day boundary produced its figures. A `DateRange`
    is deliberately naive (ADR-004) since a pre-aggregated day cannot be re-bucketed, so the
    dates in an answer are *the source's* days — and presenting them beside a UTC coverage bound
    without saying so implies a boundary that is not the one the data used.
    """

    restatement_window_days: int = 0
    """How far back this source still rewrites its own figures.

    Carried because a caveat a user must be told cannot be left to prose someone remembers.
    [answerable-questions](../../../docs/answerable-questions.md) §5 makes "these figures are
    net of returns and will move as returns arrive" mandatory for a recent-period vendor
    question, and the contract already declares the window — 45 days for vendor sales. Deriving
    the sentence from the declaration means it says *how long*, and it stops being wrong when a
    contract's window changes.

    Zero means the source does not restate, so no caveat is warranted rather than a caveat with
    a zero in it.
    """

    typed_columns: tuple[tuple[str, str], ...] = ()
    """Each column its contracts declare a non-string `target_type`, beyond the window columns.

    `(column, canonical type)`. Carried because a filter value is always bound as a quoted
    varchar (`execution.start`), and Athena refuses `date = varchar` or `bigint = varchar`
    (TYPE_MISMATCH). The adapter casts a filter on one of these columns to its type, as it does
    the window bounds: `as_of_month` (date) and `months_since_acquisition` (bigint) on cohorts.
    """

    suppression_columns: tuple[str, ...] = ()
    """The curated columns that make a privacy withholding visible, if the source has any.

    Empty for every vendor fact, because Vendor Central has no threshold mechanism — there is
    nothing to withhold and nothing to declare. Populated for the AMC facts, where
    `rows_suppressed`, `suppression_reason` and `distinct_user_count` are what turn a deleted
    row into a labelled one.

    Carried because `QueryResult.suppression` is required with no default, and an adapter
    cannot honestly state "nothing was withheld" unless it knows whether withholding was even
    possible. Absent columns are a provable absence; unread columns are an assumption.

    **Only columns the fact stores.** A shopper-aggregate contract loaded under
    `discard_suppressed_rows` declares a `suppression` block for its delivery, but no suppressed
    row reaches its fact and the discriminator is never written (ADR-099 point 7,
    `DataContract.records_cell_state`). Its withholding is disclosed per cell instead: an
    expected cell that didn't come back is stored `not_returned` (`not_returned_columns`), and a
    rostered key with no row reads "not available" (`roster`). Folding over a column the fact
    doesn't have would refuse every read of it in the engine.
    """

    partially_written_columns: tuple[str, ...] = ()
    """Columns some but not all of the contracts writing this fact supply.

    Empty for a fact with one writer, which is seven of the eight curated tables. Non-empty only
    for `fct_ad_exposure`, where four contracts each write their own measures into one table —
    impressions and spend from `amc-dsp-impressions`, clicks from `amc-dsp-clicks`, viewability
    from `amc-dsp-views`, reach from `amc-reach-frequency`.

    **A row in such a table legitimately carries no value for a column its own writer never
    supplied, and that is indistinguishable from data loss at read time.** So the distinction has
    to be declared from the contracts rather than detected from the rows. Without it, a measure
    aggregated over a group where every contributing row came from a different writer sums to
    NULL, and a NULL measure was treated as the model and the data disagreeing — ADR-037.
    """

    not_returned_columns: tuple[str, ...] = ()
    """Measures a contract declares nullable with `not_returned` (ADR-099 point 8).

    A shopper-aggregate cell that didn't come back is stored with NULL counts, so a NULL there is
    a declared absence ("too few shoppers, or none"), never a zero and never a defect.
    """

    pack_versioned_columns: tuple[PackVersionedColumn, ...] = ()
    """Columns a contract's governed pack outputs only from a later version (pack coverage)."""

    currency_sources: tuple[tuple[str, str], ...] = ()
    """Each money measure whose contract names a currency column other than `currency_code`.

    `(measure, currency column)`, from the contracts' own `currency_column` declarations (one
    home). Pack 5's window spends carry their own currencies (`dsp_currency`,
    `sponsored_currency`) beside the fact's `currency_code`, which is revenue's (ADR-021), so a
    figure of one of them is labelled from its own column, never from another measure's.
    """

    grain_sets: tuple[GrainSetShape, ...] = ()
    """A shopper-aggregate fact's served grain sets, one per writing contract (ADR-099 pt 2).

    Empty for every other fact. Each grain set is its own contract with its own grain, so a plan
    accounts for the grain of the grain set its `grain_set` filter pins (`grain_for`), never the
    union `grain_columns` holds.
    """

    def grain_set_for(self, filters: Mapping[str, str]) -> GrainSetShape | None:
        """Return the grain set a request's `grain_set` filter pins, or None."""
        pinned = filters.get(GRAIN_SET_COLUMN)
        return next((item for item in self.grain_sets if item.name == pinned), None)

    def grain_for(self, filters: Mapping[str, str]) -> tuple[str, ...]:
        """Return the grain a request must account for: its pinned grain set's, else the union.

        Unpinned, a fact with two grain sets keeps the union, which holds `grain_set` and each
        set's own columns, so the plan is refused naming them rather than reading both sets.
        """
        chosen = self.grain_set_for(filters)
        return self.grain_columns if chosen is None else chosen.grain_columns

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a fact shape requires the fact's name.")
        if not self.grain_columns:
            raise ValueError(
                f"{self.name}: grain_columns is required and must be non-empty. An empty "
                "grain would make the aggregation check pass for any plan."
            )
        if not self.columns:
            raise ValueError(
                f"{self.name}: columns is required and must be non-empty. An empty column "
                "list would make every filter key acceptable, including one that is not a "
                "column at all."
            )
        missing = tuple(c for c in self.grain_columns if c not in self.columns)
        if missing:
            raise ValueError(
                f"{self.name}: {', '.join(missing)} is in the grain but not in the column "
                "list, so the two descriptions of this fact disagree."
            )

    @classmethod
    def for_fact(cls, fact: str, contracts: Sequence[DataContract]) -> Self:
        """Describe one fact from every contract that writes it, unioning their columns.

        **`fct_ad_exposure` is written by four contracts and this is what resolves them.** Each
        supplies different measures — impressions and spend from one, clicks from another,
        viewability from a third — so any single contract's column list is a partial view of the
        table, and a plan built from one would refuse a legitimate filter on a column another
        contract writes.

        The grain is required to be identical across them, not merged. Four contracts writing one
        Iceberg table already agree on it by construction, so a disagreement means a contract was
        edited into conflict with its siblings — and picking one grain would silently change what
        makes a row unique, which is the input to ADR-024's whole check.

        Suppression columns and the source timezone come from the first contract that declares
        them, since a fact has one threshold mechanism and one business day whichever contract
        loaded the partition.
        """
        writing = [contract for contract in contracts if contract.target_table == fact]
        if not writing:
            raise ValueError(f"no contract writes {fact}, so its shape cannot be described.")

        shapes = [cls.of(contract) for contract in writing]
        grains = {shape.grain_columns for shape in shapes}
        # Disjoint grain sets of one shopper-aggregate fact are the one exception (ADR-099
        # point 2): each contract is its own grain set, stamped as a distinct `grain_set` literal,
        # so its rows never share a key with another set's and its own grain still makes a row
        # unique. Anything else that disagrees is a contract edited into conflict.
        grain_sets = tuple(item for shape in shapes for item in shape.grain_sets)
        disjoint = len(grain_sets) == len(shapes) and len(
            {item.name for item in grain_sets}
        ) == len(grain_sets)
        if len(grains) > 1 and not disjoint:
            names = ", ".join(contract.contract_id for contract in writing)
            raise ValueError(
                f"{fact}: the contracts writing it ({names}) declare {len(grains)} different "
                "grains, so what makes one row unique is ambiguous."
            )
        grain: list[str] = []
        for shape in shapes:
            grain.extend(column for column in shape.grain_columns if column not in grain)

        columns: list[str] = []
        for shape in shapes:
            for column in shape.columns:
                if column not in columns:
                    columns.append(column)

        # Which of the unioned columns fewer than all writers supply. Computed here because this
        # is the only place that knows how many contracts write the fact — a shape built from one
        # contract cannot tell a column it owns from a column it is the sole writer of. Empty
        # whenever there is a single writer, which preserves the invariant for every other fact.
        partial = tuple(
            column for column in columns if any(column not in shape.columns for shape in shapes)
        )

        return cls(
            name=fact,
            grain_columns=tuple(grain),
            columns=tuple(columns),
            source_timezone=next((s.source_timezone for s in shapes if s.source_timezone), ""),
            restatement_window_days=max(shape.restatement_window_days for shape in shapes),
            typed_columns=tuple(
                dict.fromkeys(item for shape in shapes for item in shape.typed_columns)
            ),
            suppression_columns=next(
                (s.suppression_columns for s in shapes if s.suppression_columns), ()
            ),
            partially_written_columns=partial,
            not_returned_columns=tuple(
                dict.fromkeys(column for shape in shapes for column in shape.not_returned_columns)
            ),
            pack_versioned_columns=tuple(
                dict.fromkeys(column for shape in shapes for column in shape.pack_versioned_columns)
            ),
            currency_sources=tuple(
                dict.fromkeys(item for shape in shapes for item in shape.currency_sources)
            ),
            grain_sets=grain_sets,
        )

    @classmethod
    def of(cls, contract: DataContract) -> Self:
        """Describe the fact one contract writes, including the columns it does not map.

        `target_columns` walks field mappings, so it omits `basis_key` and `source_timezone` —
        those are projected from contract-level metadata. Omitting them here would refuse a
        legitimate filter on the measurement basis, so they are added back for a fact.
        """
        projected = CONFORMED_PROVENANCE_COLUMNS if contract.targets_a_fact else ()
        suppression = contract.suppression
        return cls(
            name=contract.target_table,
            grain_columns=contract.grain,
            columns=(*contract.target_columns, *projected),
            source_timezone=contract.source_timezone,
            restatement_window_days=contract.restatement_window_days,
            typed_columns=tuple(
                (field.target_column, field.target_type)
                for field in contract.fields
                if field.target_type is not None
                and field.target_type != CanonicalType.STRING.value
                and field.target_column not in (DATE_COLUMN, PERIOD_START_COLUMN, PERIOD_END_COLUMN)
            ),
            # Already curated column names in the contract, not source paths, so no mapping
            # is needed — `discriminator_column: rows_suppressed` names the target directly.
            # Only a stored discriminator is folded over: a `discard_suppressed_rows` contract
            # writes none (see the field's docstring).
            suppression_columns=()
            if suppression is None
            or suppression.discriminator_column not in contract.target_columns
            else (
                suppression.discriminator_column,
                suppression.reason_column,
                suppression.distinct_user_count_column,
            ),
            not_returned_columns=tuple(
                field.target_column
                for field in contract.fields
                if field.nullable and field.null_meaning is NullMeaning.NOT_RETURNED
            ),
            pack_versioned_columns=pack_versioned_columns((contract,)),
            currency_sources=tuple(
                (field.target_column, field.currency_column)
                for field in contract.monetary_fields
                if field.currency_column and field.currency_column != CURRENCY_COLUMN
            ),
            grain_sets=GrainSetShape.of(contract),
        )


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """A question, in the vocabulary the metric dictionary uses.

    `grains` holds dimension *names* as the dictionary declares them, not column names, so a
    caller never needs to know the canonical schema. `filters` holds canonical columns, since
    a filter pins something the dimension vocabulary has no name for — `distributor_view` is
    a grain column and not a dimension anyone asks a question about.
    """

    metric_name: str
    grains: tuple[str, ...]
    window: DateRange
    filters: Mapping[str, str] = field(default_factory=dict[str, str])
    pack_coverage: tuple[PackDayVersions, ...] | None = None
    """Per-day query pack versions for the contracts whose pack-versioned columns this reads.

    Evidence read from publication state by the caller's adapter, never a choice: a metric that
    reads a column a later pack added refuses a window with any uncovered day, and refuses
    outright when this is None (`domain.measurement.pack_coverage`). Every other metric ignores it.
    """

    roster: Roster | None = None
    """The roster a roster-graded grain set's figure needs, for exactly this window (plan D4).

    Evidence read from `fct_ad_exposure` publications by the caller's adapter
    (`ports.outbound.rosters`). A figure at such a grain set refuses without it
    (`domain.measurement.rosters.require_roster`); every other figure ignores it.
    """

    def __post_init__(self) -> None:
        """Refuse a malformed question, classified so the refusal reaches the caller.

        **`ValidationError` and not `ValueError`, and the difference was a live defect.** These are
        caller mistakes arriving off the wire, not broken invariants — so every one of them is a
        question this platform can decline and explain. A bare `ValueError` is not an
        `AdlyticsError`, so `measurement_entrypoint.handler` re-raised it, Lambda reported a
        failure, and Gateway told the model the tool was broken. The message already named every
        grain that would have worked and the model never saw one of them: it spent three further
        tool calls rediscovering `product` through the catalog, and on `measurement_top_n` it never
        recovered at all because each attempt looked like an outage rather than an answer
        [verified 2026-08-22 against deployed dev].

        The known dimensions go in `context` as well as the description, because context is what a
        structured refusal carries into the log and the span — and a refusal that cannot be
        diagnosed later is only half an answer.
        """
        if not self.metric_name:
            raise ValidationError(
                code=UNKNOWN_DIMENSION,
                description="A measurement needs a metric_name.",
            )
        if not self.grains:
            raise ValidationError(
                code=UNKNOWN_DIMENSION,
                description=(
                    "At least one grain is required. A figure at an unstated grain is the "
                    f"failure mode this product must not have. Available: "
                    f"{', '.join(sorted(GRAIN_COLUMNS))}."
                ),
                context={"available": sorted(GRAIN_COLUMNS)},
            )
        unknown = set(self.grains) - set(GRAIN_COLUMNS)
        if unknown:
            raise ValidationError(
                code=UNKNOWN_DIMENSION,
                description=(
                    "One or more requested grains are unsupported. Use one of: "
                    f"{', '.join(sorted(GRAIN_COLUMNS))}. Use dimension names rather than "
                    "result column names."
                ),
                context={
                    "outcome": "unsupported_grain",
                    "n": len(unknown),
                    "available": sorted(GRAIN_COLUMNS),
                },
            )


@dataclass(frozen=True, slots=True)
class FactShapes:
    """Both facts a drill-across reads.

    Passed where a single `FactShape` would go, so a caller answering a cross-fact ratio supplies
    two shapes and every other caller is unchanged. That is the opt-in: a question needing a join
    describes both facts, and a question that does not never mentions the second.
    """

    numerator: FactShape
    denominator: FactShape

    def __post_init__(self) -> None:
        if self.numerator.name == self.denominator.name:
            raise ValueError(
                f"both sides describe {self.numerator.name}, which is one grouped query rather "
                "than a join."
            )


@dataclass(frozen=True, slots=True)
class JoinSide:
    """The second fact of a drill-across, with everything specific to it.

    A ratio across two facts cannot be one grouped query. Each side aggregates its own fact to
    the dimensions they share, and the results are joined — so the denominator needs its own
    fact, its own column and **its own filters**, because the two facts have different grain
    columns and ADR-024's accounting therefore runs per side. `fct_ad_exposure` needs a channel
    and an ad product pinned; `fct_attributed_conversion` needs an ASIN accounted for. Neither
    list is valid for the other.

    Deliberately holds its own `measure` rather than the plan carrying both columns in `measures`.
    A `measures[1]` that belonged to a different fact than `fact` would be a trap for every
    reader and every adapter.
    """

    fact: str
    measure: str
    filters: Mapping[str, str]
    source_datasets: tuple[str, ...]
    carried_columns: tuple[str, ...] = ()
    suppression_columns: tuple[str, ...] = ()
    """The fact's suppression columns, if it has any.

    Load-bearing for the join rather than only for reporting. A key present on the other side and
    absent here is either "this fact measured nothing" or "Amazon withheld it", and those demand
    opposite answers — zero is a fact in the first case and a fabrication in the second. Non-empty
    here is what lets the query tell them apart.
    """

    periodic: bool = False
    """This side reads a periodic fact, selected at the plan's exact period (ADR-100)."""
    typed_columns: tuple[tuple[str, str], ...] = ()
    """This side's non-string columns and their types. See `FactShape.typed_columns`."""

    def __post_init__(self) -> None:
        if not self.source_datasets:
            raise ValueError("a drill-across side requires its leaf source datasets.")


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A validated read, carrying what the adapter needs and what the answer must declare.

    Both halves matter. The adapter reads `fact`, `measures`, `group_by`, `window` and
    `filters` to compose SQL. The tool reads `grain`, `units`, `measurement_basis` and
    `facts_read` to build a `ShapedResponse` without inventing any of them — which is what
    stops a response from stating a grain the query did not actually produce.
    """

    metric: Metric
    fact: str
    measures: tuple[str, ...]
    """The columns to aggregate, in the order `combination` reads them.

    A tuple rather than one column because three plan kinds land here: one aggregate reported
    as-is, two subtracted, two divided. All are a single query over a single fact, so all come
    back in one row — which is why this is a wider plan rather than a second plan type.
    """

    group_by: tuple[str, ...]
    window: DateRange
    filters: Mapping[str, str]
    source_datasets: tuple[str, ...]
    grain: Grain
    facts_read: tuple[str, ...]
    measurement_basis: tuple[MeasurementBasis, ...]
    units: UnitKind

    semantic_qualifiers: tuple[CompatibilityDifference, ...] = ()
    """Structured compatibility differences accepted by the metric declaration."""

    combination: Combination = Combination.SINGLE
    """How the aggregates become one figure. Declared by the dictionary, never inferred."""

    join: JoinSide | None = None
    """The denominator's fact, when this quotient spans two of them.

    Present exactly for a drill-across. When it is set, `fact`, `measures` and `filters` describe
    the numerator side only and this describes the denominator side — so every column sits beside
    the fact that declares it.
    """

    multiplier: int = 1
    """A scalar the combined figure is scaled by, carried from the metric.

    `cpm` is the only user. It rides on the plan so the arithmetic happens in domain code with
    the factor the declaration stated, rather than in a tool that found a thousand somewhere.
    """

    source_timezone: str = ""
    """The zone the fact's business day closes in. See `FactShape.source_timezone`."""

    restatement_window_days: int = 0
    """How far back the fact this plan reads still rewrites its own figures.

    Carried so the answer can state the caveat with a duration rather than as remembered prose.
    See `FactShape.restatement_window_days`.
    """

    partially_written_columns: tuple[str, ...] = ()
    """Measures that may legitimately be absent from a row. See `FactShape`'s field of this name.

    Carried on the plan because `measure()` is where a NULL aggregate is read, and it needs to
    tell a column no writer supplied for this group from one that should have had a value.
    """
    not_returned_columns: tuple[str, ...] = ()
    """Measures whose NULL is a declared `not_returned` cell. See `FactShape`'s field."""
    typed_columns: tuple[tuple[str, str], ...] = ()
    """The primary fact's non-string columns and their types. See `FactShape.typed_columns`."""

    suppression_columns: tuple[str, ...] = ()
    """The fact's suppression columns, carried through from its shape.

    An adapter needs this to fill `QueryResult.suppression`, which has no default. Empty means
    the source has no threshold mechanism, so nothing could have been withheld — a provable
    absence. Non-empty means the adapter must read them rather than assume, and one that
    cannot should refuse instead of claiming nothing was withheld.
    """

    carried_columns: tuple[str, ...] = ()
    """Columns the query must return that are not part of the reported grain.

    `currency_code` for a monetary metric, and nothing else today. It labels the figure
    rather than slicing it, which is why it is separate from `group_by`: the answer's grain
    is the dimensions someone asked for, and adding a currency to it would describe a
    breakdown nobody requested.

    SQL still requires them in the `GROUP BY`, and that is the useful consequence. If
    currency genuinely varies within the requested grain, the query returns two rows for one
    ASIN instead of silently collapsing them, and `measure()` can refuse a figure whose
    currency is ambiguous rather than label it with whichever value came back first.
    """

    primary_carried_columns: tuple[str, ...] = ()
    """Which output carried columns physically exist on the primary drill-across side."""

    currency_source: str = CURRENCY_COLUMN
    """The fact column the carried `currency_code` is read from.

    `currency_code` for every money measure but one whose contract names its own currency column
    (`FactShape.currency_sources`): pack 5's DSP window spend reads `dsp_currency`, its sponsored
    window spend `sponsored_currency`. The adapter selects that column as `currency_code`, so the
    answer still carries one currency under one name and nothing downstream reads another.
    Only a plan with one money measure on one fact reads another source (a single measure, or a
    quotient of money over a count on a fact with no `currency_code`, pack 8's CPM); a plan
    combining measures whose currencies are different columns is refused before it is built.
    """

    period: CalendarPeriod | None = None
    """The exact complete period a periodic side reads (ADR-100, D-1, D-7).

    Set exactly when a side is periodic, and then equal to `window`. A periodic side selects
    `period_start = start AND period_end = end`; a daily side over the same plan sums the
    period's days (D-6).
    """

    periodic: bool = False
    """The primary side reads a periodic fact."""

    roster: Roster | None = None
    """The roster a roster-graded grain set's figure is read against (`domain.measurement.rosters`).

    Carried so the answer, and a bake reading it, name every rostered key with no row as "not
    available" and grade the loss with one rule.
    """

    def __post_init__(self) -> None:
        """Check the aggregate count matches what the combination can consume.

        A mismatch is unreachable through `plan()` and would be silent if it happened: a
        quotient built from one aggregate has nothing to divide by, and a single built from two
        would report the first and discard the second.
        """
        if not self.source_datasets:
            raise ValueError(f"{self.metric.name}: query plan requires leaf source datasets.")
        reads_periodic = self.periodic or (self.join is not None and self.join.periodic)
        if reads_periodic != (self.period is not None):
            raise ValueError(
                f"{self.metric.name}: a plan carries an exact period exactly when a side is "
                "periodic."
            )
        if self.period is not None and self.period.window != self.window:
            raise ValueError(f"{self.metric.name}: the exact period must equal the window.")
        if self.join is not None:
            if self.combination is not Combination.QUOTIENT:
                raise ValueError(
                    f"{self.metric.name}: only a quotient joins a second fact, and this is a "
                    f"{self.combination.value}."
                )
            if len(self.measures) != 1:
                raise ValueError(
                    f"{self.metric.name}: a drill-across aggregates one column per side, so this "
                    f"side needs exactly 1 measure, got {len(self.measures)}."
                )
            if self.join.fact == self.fact:
                raise ValueError(
                    f"{self.metric.name}: both sides read {self.fact}, so this is one grouped "
                    "query rather than a join."
                )
            available_carried = {
                *self.primary_carried_columns,
                *self.join.carried_columns,
            }
            if set(self.carried_columns) != available_carried:
                raise ValueError(
                    f"{self.metric.name}: drill-across output carried columns must equal the "
                    "columns available from its two sides."
                )
            if set(self.carried_columns) - {CURRENCY_COLUMN}:
                raise ValueError(
                    f"{self.metric.name}: drill-across carries only {CURRENCY_COLUMN}."
                )
            return

        if self.primary_carried_columns:
            raise ValueError(
                f"{self.metric.name}: primary_carried_columns apply only to drill-across."
            )
        if self.currency_source != CURRENCY_COLUMN and (
            CURRENCY_COLUMN not in self.carried_columns
            or self.combination is Combination.DIFFERENCE
        ):
            # A quotient is allowed only where `_currency_source` found one money operand (a
            # segment's spend over its impressions); a difference subtracts two amounts.
            raise ValueError(
                f"{self.metric.name}: a currency read from {self.currency_source} labels one "
                f"carried {CURRENCY_COLUMN} on a plan with one money measure."
            )
        expected = 1 if self.combination is Combination.SINGLE else 2
        if len(self.measures) != expected:
            raise ValueError(
                f"{self.metric.name}: a {self.combination.value} plan needs exactly "
                f"{expected} measure(s), got {len(self.measures)}."
            )

    @property
    def measure_column(self) -> str:
        """The first aggregated column.

        Kept for the error contexts that name what failed, where the useful detail is which
        fact column was involved rather than the whole expression.
        """
        return self.measures[0]

    @property
    def accounted_columns(self) -> frozenset[str]:
        """Every grain column this plan groups by, filters, or bounds with its window."""
        return frozenset({*self.group_by, *self.filters, DATE_COLUMN})

    @property
    def selected_columns(self) -> tuple[str, ...]:
        """Every column the query returns, in a stable order, excluding the measure."""
        return (*self.group_by, *self.carried_columns)

    @property
    def result_aliases(self) -> tuple[str, ...]:
        """The trailing expressions a result row carries, in the order the statement selects them.

        Read by the adapter instead of a count, because a drill-across interleaves its aggregates
        with their suppression indicators — `value_0, suppressed_0, value_1, suppressed_1` — and a
        count cannot express that ordering. Owned here rather than in the adapter so the composer
        and the row mapper read one statement of the shape.
        """
        if self.join is not None:
            return tuple(
                alias
                for index in (0, 1)
                for alias in (
                    measure_alias(index),
                    withheld_alias(index),
                    withheld_rows_alias(index),
                )
            )
        # Every measure carries its own withheld pair when the fact can withhold, and none when it
        # cannot — a vendor fact has no threshold mechanism, so selecting a zero would be an
        # expression with nothing behind it. ADR-035.
        if not self.suppression_columns:
            return tuple(measure_alias(index) for index in range(len(self.measures)))
        return tuple(
            alias
            for index in range(len(self.measures))
            for alias in (
                measure_alias(index),
                withheld_alias(index),
                withheld_rows_alias(index),
            )
        )


def _signature(metric: Metric, source_timezone: str) -> CompatibilitySignature:
    return CompatibilitySignature(
        business_process=metric.measurement_basis[0].value,
        metric_semantics=metric.name,
        measurement_basis=metric.measurement_basis[0],
        measurement_method=metric.source_system,
        population=metric.population,
        source_timezone=source_timezone,
        business_calendar=metric.business_calendar,
        time_class=metric.time_class,
        time_grain=metric.time_grain,
        currency=("marketplace_currency" if metric.unit_kind is UnitKind.CURRENCY else None),
        profile_version=metric.profile_version,
        attribution_window_days=metric.attribution_window_days,
        lookback_months=metric.lookback_months,
    )


@dataclass(frozen=True, slots=True)
class _QualifierRequest:
    catalog: Catalog
    metric: Metric
    shapes: FactShapes | None = None
    source_timezone: str = "same_fact"


def _semantic_qualifiers(
    request: _QualifierRequest,
) -> tuple[CompatibilityDifference, ...]:
    catalog = request.catalog
    metric = request.metric
    shapes = request.shapes
    source_timezone = request.source_timezone
    qualifiers: list[CompatibilityDifference] = []
    if metric.kind is MetricKind.RATIO:
        numerator = catalog.metric(metric.numerator)
        denominator = catalog.metric(metric.denominator)
        if numerator is not None and denominator is not None:
            left_timezone = source_timezone if shapes is None else shapes.numerator.source_timezone
            right_timezone = (
                source_timezone if shapes is None else shapes.denominator.source_timezone
            )
            allowed_fields: set[CompatibilityField] = set()
            if metric.compatibility_mode is MetricCompatibilityMode.QUALIFIED_RATIO:
                allowed_fields.update(
                    {
                        CompatibilityField.METRIC_SEMANTICS,
                        CompatibilityField.ATTRIBUTION_WINDOW,
                        CompatibilityField.LOOKBACK_WINDOW,
                        CompatibilityField.CURRENCY,
                    }
                )
            if (
                metric.compatibility_mode is MetricCompatibilityMode.QUALIFIED_RATIO
                and metric.declared_mixes_basis
            ):
                allowed_fields.update(
                    {
                        CompatibilityField.BUSINESS_PROCESS,
                        CompatibilityField.MEASUREMENT_BASIS,
                        CompatibilityField.MEASUREMENT_METHOD,
                        CompatibilityField.SOURCE_TIMEZONE,
                    }
                )
            allowed = frozenset(allowed_fields)
            left, right = _aligned_to_period(
                catalog,
                (numerator, _signature(numerator, left_timezone)),
                (denominator, _signature(denominator, right_timezone)),
            )
            try:
                result = validate_composition(
                    left, right, CompositionRule(CompositionOperation.RATIO, allowed, allowed)
                )
            except SemanticCompatibilityError as error:
                raise Refusal(
                    requested=metric.name,
                    reason=RefusalReason.GRAIN_NOT_SUPPORTED,
                    explanation=str(error),
                ).to_error() from None
            qualifiers.extend(result.differences)
            if numerator.fee_inclusive != denominator.fee_inclusive:
                if metric.compatibility_mode is MetricCompatibilityMode.STRICT:
                    raise Refusal(
                        requested=metric.name,
                        reason=RefusalReason.GRAIN_NOT_SUPPORTED,
                        explanation=(
                            "strict ratio would erase incompatible semantics: fee_inclusion."
                        ),
                    ).to_error()
                qualifiers.append(
                    CompatibilityDifference(
                        CompatibilityField.FEE_INCLUSION,
                        numerator.fee_inclusive,
                        denominator.fee_inclusive,
                    )
                )
    return tuple(qualifiers)


def _aligned_to_period(
    catalog: Catalog,
    left: tuple[Metric, CompatibilitySignature],
    right: tuple[Metric, CompatibilitySignature],
) -> tuple[CompatibilitySignature, CompatibilitySignature]:
    """Align a daily-additive operand to its periodic partner's time semantics (D-6, ADR-048).

    The plan sums the daily side over exactly the period's days, so after alignment both sides
    measure the same period: the time class and grain differ only before the sum, and stating
    them as a difference would refuse the one composition D-6 allows. Any other pairing is left
    as declared, so the composition rule still sees it.
    """
    (left_metric, left_signature), (right_metric, right_signature) = left, right
    left_periodic = bool(periodic_leaves(catalog, left_metric))
    right_periodic = bool(periodic_leaves(catalog, right_metric))
    if left_periodic == right_periodic:
        return left_signature, right_signature
    daily, periodic = (
        (right_signature, left_signature) if left_periodic else (left_signature, right_signature)
    )
    daily_metric = right_metric if left_periodic else left_metric
    if daily_metric.time_class is not FactTimeClass.DAILY_ADDITIVE:
        return left_signature, right_signature
    aligned = replace(daily, time_class=periodic.time_class, time_grain=periodic.time_grain)
    return (left_signature, aligned) if left_periodic else (aligned, right_signature)


RATIO_OPERAND_COUNT = 2
"""Every declared ratio has exactly one numerator and one denominator policy."""


type _LeafOperand = tuple[AggregationPolicy, frozenset[PlatformKey]]
"""One leaf's aggregation policy and the platforms its figures can come from (ADR-106)."""


def _leaf_operands(catalog: Catalog, metric: Metric) -> tuple[_LeafOperand, ...]:
    """Each distinct leaf, keyed by policy *and* platforms.

    Keyed by both so two operands with equal policies but different source platforms stay
    separate: each gets its own accounted axes.
    """
    if metric.kind is MetricKind.SIMPLE:
        if metric.aggregation_policy is None:  # pragma: no cover — catalog construction rejects it
            raise CatalogError(f"{metric.name} has no aggregation policy.")
        return ((metric.aggregation_policy, metric.source_platforms),)
    found: list[_LeafOperand] = []
    for name in (metric.numerator, metric.denominator):
        operand = catalog.metric(name)
        if operand is None:  # pragma: no cover — catalog construction rejects it
            raise CatalogError(f"{metric.name} has unknown operand {name}.")
        for leaf in _leaf_operands(catalog, operand):
            if leaf not in found:
                found.append(leaf)
    return tuple(found)


def _leaf_accounted(
    accounted: frozenset[SemanticAxis], platforms: frozenset[PlatformKey]
) -> frozenset[SemanticAxis]:
    """Account both network axes for a leaf that can only come from one platform (ADR-106).

    One platform means one ecosystem, so the leaf can't be summed across either axis: leaving
    them out of the request is not a roll-up. A leaf with two or more platforms, or none known,
    keeps its own rule on each omitted network axis.
    """
    if len(platforms) == 1:
        return accounted | NETWORK_AXES
    return accounted


def _network_first(omitted: frozenset[SemanticAxis]) -> tuple[frozenset[SemanticAxis], ...]:
    """Split omitted axes so a cross-network refusal names the platform axis.

    Checking the parts in turn passes or fails exactly as checking them together would; only
    which refusal is reported first changes. A single-platform leaf never omits a network axis
    (`_leaf_accounted`), so its refusal text is the same either way.
    """
    platform = omitted & {SemanticAxis.PLATFORM}
    return (platform, omitted - platform)


def _validate_semantic_axes(  # noqa: PLR0913, PLR0917 — the pinned grain set is optional
    catalog: Catalog,
    metric: Metric,
    request: PlanRequest,
    grain_set: GrainSetShape | None = None,
) -> None:
    # A periodic metric reaches here only at one exact measured period (`require_exact_period`).
    exact_period = bool(periodic_leaves(catalog, metric))
    groups = frozenset(_GRAIN_AXES[name] for name in request.grains)
    filters = frozenset(
        axis for column in request.filters if (axis := _COLUMN_AXES.get(column)) is not None
    )
    accounted = groups | filters | _outside_grain_set(grain_set)
    if SemanticAxis.CHANNEL in accounted or SemanticAxis.AD_PRODUCT in accounted:
        accounted |= frozenset(
            {
                SemanticAxis.CHANNEL_GROUP,
                SemanticAxis.PLATFORM,
                SemanticAxis.ECOSYSTEM,
            }
        )
    elif SemanticAxis.CHANNEL_GROUP in accounted:
        accounted |= frozenset({SemanticAxis.PLATFORM, SemanticAxis.ECOSYSTEM})
    elif SemanticAxis.PLATFORM in accounted:
        accounted |= frozenset({SemanticAxis.ECOSYSTEM})
    if request.window.days == 1 or exact_period:
        # One exact period is one measured row set, like one day: nothing is summed across time.
        # A daily operand over it sums the period's days, which only an additive one may (D-6).
        accounted |= frozenset({SemanticAxis.TIME})
    leaves = _leaf_operands(catalog, metric)
    if metric.kind is MetricKind.RATIO and len(leaves) == RATIO_OPERAND_COUNT:
        (numerator, numerator_platforms), (denominator, denominator_platforms) = leaves
        ratio = RatioOfAggregatesPolicy(numerator, denominator)
        numerator_omitted = numerator.available_axes - _leaf_accounted(
            accounted, numerator_platforms
        )
        denominator_omitted = denominator.available_axes - _leaf_accounted(
            accounted, denominator_platforms
        )
        try:
            ratio.require_capabilities(groups, filters)
            for numerator_part, denominator_part in zip(
                _network_first(numerator_omitted),
                _network_first(denominator_omitted),
                strict=True,
            ):
                ratio.require_operand_rollup(numerator_part, denominator_part)
        except SemanticCompatibilityError as error:
            raise Refusal(
                requested=metric.name,
                reason=RefusalReason.GRAIN_NOT_SUPPORTED,
                explanation=str(error),
                offer_instead=metric.valid_grains,
            ).to_error() from None
        return
    for policy, platforms in leaves:
        try:
            policy.require_groupable(groups)
            if metric.kind is MetricKind.RATIO:
                policy.require_alignable(groups)
            policy.require_filterable(filters & policy.available_axes)
            omitted = policy.available_axes - _leaf_accounted(accounted, platforms)
            for part in _network_first(omitted):
                if part:
                    policy.require_rollup(part, operation=AggregationOperation.SUM)
        except SemanticCompatibilityError as error:
            raise Refusal(
                requested=metric.name,
                reason=RefusalReason.GRAIN_NOT_SUPPORTED,
                explanation=str(error),
                offer_instead=metric.valid_grains,
            ).to_error() from None


def _refuse_a_ratio_the_tool_pairs(metric: Metric) -> None:
    """Refuse a ratio whose operands no one query's row holds; the measurement tool pairs them."""
    if metric.cross_delivery is CrossDeliveryException.ARP_COHORT_RATIO:
        # Its operands sit on two deliveries' rows (pack 15's arp, pack 14's standard), so no one
        # query reads both. The measurement tool plans each operand on its own path and pairs
        # them under the cohort-size check (`answer_arp_rate`); a single plan never divides them.
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} divides the paid path's {metric.numerator} by the standard "
                f"path's {metric.denominator}, two deliveries one query can't read; the "
                "measurement tool pairs them cell by cell."
            ),
            offer_instead=(metric.numerator, metric.denominator),
        ).to_error()
    if metric.delivery_ratio is DeliveryRatio.SEGMENT_PROFILE:
        # Its operands sit on two rows of one delivery, a segment row and the profiled_total row,
        # so dividing them inside one query's row would divide by an empty total. The
        # measurement tool plans each operand and pairs them per segment
        # (`answer_segment_profile_ratio`); a single plan never divides them.
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} divides a segment row's figure by its delivery's profiled total, "
                "two rows one query can't read; the measurement tool pairs them per segment."
            ),
            offer_instead=(metric.numerator, metric.denominator),
        ).to_error()


def plan(catalog: Catalog, request: PlanRequest, shape: FactShape | FactShapes) -> QueryPlan:
    """Validate a question against the dictionary and the fact's grain, or raise.

    `shape` is one `FactShape` for every question over a single fact, or a `FactShapes` carrying
    both when the metric is a ratio whose operands live in two of them. A caller passing one shape
    to a cross-fact ratio gets the drill-across refusal, which is what lets the tool adopt this
    incrementally — supplying the second shape is the opt-in.

    Raises rather than returning a refusal, for the reason `Refusal.to_error` gives: a
    refusal ran no query, so a response envelope would have to invent a measurement basis
    and a coverage window for figures that do not exist.

    Order is deliberate. The dictionary's own refusals come first — an unknown metric must
    not be reported as a grain problem — and the aggregation check comes last, because it is
    the only one whose answer depends on the fact rather than the request.
    """
    # Normalised once, so every check below reads one shape and only the drill-across path knows
    # there can be two.
    shapes = shape if isinstance(shape, FactShapes) else None
    single = shape.numerator if isinstance(shape, FactShapes) else shape

    for grain_name in request.grains:
        # The filters reach the dictionary because a `requires` option is satisfied by what
        # the question pinned, and the reporting-lens defaults are already merged into
        # `request.filters` by the time a tool calls this. Without them every metric declaring
        # a source option refuses regardless of the data.
        refusal = catalog.refusal_for(
            request.metric_name, grain=grain_name, filters=request.filters
        )
        if refusal is not None:
            raise refusal.to_error()

    metric = catalog.metric(request.metric_name)
    if metric is None:  # pragma: no cover — refusal_for already raised UNKNOWN_METRIC
        raise Refusal(
            requested=request.metric_name,
            reason=RefusalReason.UNKNOWN_METRIC,
            explanation=f"{request.metric_name} is not a metric this platform declares.",
        ).to_error()
    # A request with no breakdown reaches no grain check, so a ratio computed across one
    # delivery's cells is refused here too (design §6.3).
    within = catalog.within_delivery_refusal(metric)
    if within is not None:
        raise within.to_error()
    _refuse_a_ratio_the_tool_pairs(metric)

    all_columns = set(single.columns)
    if shapes is not None:
        all_columns.update(shapes.denominator.columns)
    unknown_filters = sorted(set(request.filters) - all_columns)
    if unknown_filters:
        raise ValidationError(
            code=UNFILTERABLE_COLUMN,
            description=f"{', '.join(unknown_filters)} cannot be filtered by these facts.",
            context={"unknown_filters": unknown_filters},
        )
    pinned_set = next(
        (
            chosen
            for side in (single, *(() if shapes is None else (shapes.denominator,)))
            if (chosen := side.grain_set_for(request.filters)) is not None
        ),
        None,
    )
    period = require_exact_period(
        PeriodicPlanning(
            catalog,
            metric,
            request.window,
            request.filters,
            grain_set_periods=(
                None
                if pinned_set is None or not pinned_set.period_grains
                else (pinned_set.name, pinned_set.period_grains)
            ),
        )
    )
    # Every caller plans through here, so chat, page bakes and insights inherit the pack-version
    # coverage gate; a metric reading no pack-versioned column is untouched.
    require_pack_coverage(
        PackCoverageRequest(
            metric_name=metric.name,
            subject=metric.label,
            window=request.window,
            columns=_pack_columns(
                catalog, metric, (single, *(() if shapes is None else (shapes.denominator,)))
            ),
            coverage=request.pack_coverage,
            filters=request.filters,
            # The money a figure is denominated in is grouped beside its grain, so a late
            # writer that doesn't write it can't supply an operand (`SPLIT_WRITER_REFUSED`).
            # Only the currency is carried; `split_writer_text` words its refusal for it.
            carried=(
                (CURRENCY_COLUMN,)
                if metric.unit_kind is UnitKind.CURRENCY and CURRENCY_COLUMN in all_columns
                else ()
            ),
        )
    )
    across = _drill_across(catalog, metric)
    if across is not None:
        return _plan_drill_across(
            _DrillRequest(
                catalog=catalog, request=request, shapes=shapes, across=across, period=period
            )
        )
    if shapes is not None:  # pragma: no cover — a guard, not a path a caller reaches by design
        raise ValidationError(
            code=UNFILTERABLE_COLUMN,
            description=(
                f"{metric.name} reads one fact, so a second shape describes nothing it queries."
            ),
            context={"metric": metric.name},
        )

    fact, measures, combination = _measures_of(catalog, metric)

    # Every aggregated column must be one the fact declares. Checked for the same reason filter
    # keys are: an adapter interpolates these identifiers into the statement, so the plan is what
    # establishes they came from a contract.
    #
    # This went unchecked and it mattered. `ctr` divides clicks by impressions, and **no single
    # contract writing `fct_ad_exposure` declares both** — `amc-dsp-clicks` supplies clicks,
    # `amc-dsp-impressions` supplies impressions. A shape built from one contract accepted a plan
    # naming the other's column, and the SQL then worked anyway because the union of the four
    # contracts is what defines the table. So the plan was right by luck and unverifiable.
    # `FactShape.for_fact` is the fix on the caller's side; this is the check that says so.
    undeclared = tuple(column for column in measures if column not in single.columns)
    if undeclared:
        raise ValidationError(
            code=UNFILTERABLE_COLUMN,
            description=(
                f"{metric.name} aggregates {', '.join(undeclared)}, which {single.name} does not "
                "declare. Build the shape from every contract writing the fact."
            ),
            context={
                "fact": single.name,
                "metric": metric.name,
                "undeclared_measures": sorted(undeclared),
            },
        )

    # Every filter key must be a real column of this fact. This is a safety boundary, not a
    # tidiness one: a filter key is an identifier, SQL cannot bind an identifier, so whichever
    # adapter renders this plan has to interpolate it into the statement. Validating here means
    # the adapter interpolates only names the contract declares, and binds only values.
    unknown = tuple(column for column in request.filters if column not in single.columns)
    if unknown:
        # **Names the columns that would work, and that is a latency fix as much as a courtesy.**
        # This refusal used to say only what was wrong. Measured against the deployed harness, a
        # model asked for `marketplace_id`, was told no, guessed `marketplace`, was told no again,
        # and only then went to the catalog — three model round trips at four to five seconds each
        # to discover `marketplace_key` [verified 2026-08-22]. The dimension refusal above lists its
        # alternatives and cost one round trip for the same class of mistake. A refusal that states
        # what is wrong without stating what would work is not a cheap refusal; every round trip
        # re-sends the whole transcript, so it costs latency and an ever-larger input bill.
        raise ValidationError(
            code=UNFILTERABLE_COLUMN,
            description=(
                f"{', '.join(unknown)} is not a column of {single.name}, so it cannot be filtered. "
                f"Pinnable columns on this fact: {', '.join(sorted(single.grain_columns))}."
            ),
            context={
                "fact": single.name,
                "unknown_filters": sorted(unknown),
                "pinnable": sorted(single.grain_columns),
            },
        )

    # Rebuilt from the fact's own column tuple rather than reused from the request. Validating
    # the key would already refuse anything hostile, but this makes the guarantee structural:
    # the identifier an adapter interpolates is provably a string from the contract, not a
    # string a caller supplied that happened to compare equal to one. Values are untouched —
    # they are bound, never interpolated.
    filters = {
        declared: request.filters[declared]
        for declared in single.columns
        if declared in request.filters
    }

    group_by = tuple(GRAIN_COLUMNS[name] for name in request.grains)
    grain_set = single.grain_set_for(request.filters)
    _refuse_a_split_the_grain_set_lacks(metric, single, request)
    accounted = frozenset(
        {
            *group_by,
            *filters,
            DATE_COLUMN,
            *_folded_columns(single.suppression_columns),
            *((PERIOD_START_COLUMN, PERIOD_END_COLUMN) if period is not None else ()),
            # A pinned row kind's rows hold one NULL in a grain column they never carry (a C2
            # profiled_total row's segment_id), so nothing is summed across it.
            *(() if grain_set is None else grain_set.absent_on(request.filters)),
        }
    )
    additive_rollups = _additive_rollup_columns(catalog, metric.name)
    unaccounted = tuple(
        column
        for column in single.grain_for(request.filters)
        if column not in accounted and column not in additive_rollups
    )

    if unaccounted:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.GRAIN_NOT_SUPPORTED,
            explanation=(
                f"{metric.name} over {single.name} would sum across "
                f"{', '.join(unaccounted)}, which the fact's grain says makes a row "
                "distinct. Summing across a grain column is a claim that it is addition, "
                "and for an extraction-option column it is not — a MANUFACTURING pull and a "
                "SOURCING pull describe the same units under two lenses. Group by each "
                "column or filter it."
            ),
            # Deliberately no `offer_instead`: it renders as "Available instead", which would
            # read as alternative metrics rather than columns to pin. The explanation already
            # names them and states the action.
        ).to_error()

    _refuse_aggregating_a_distinct_count(
        _AdditivityCheck(
            catalog=catalog,
            metric=metric,
            request=request,
            shape=single,
            group_by=group_by,
            exact_period=period is not None,
        )
    )
    _validate_semantic_axes(catalog, metric, request, grain_set if period is not None else None)
    roster = require_roster(
        (
            None
            if grain_set is None or period is None
            else grain_set.roster_for(group_by, request.filters),
            request.roster,
            request.window,
            request.filters,
        )
    )

    return QueryPlan(
        metric=metric,
        fact=fact,
        measures=measures,
        combination=combination,
        multiplier=metric.multiplier,
        group_by=group_by,
        window=request.window,
        filters=filters,
        source_datasets=_leaf_source_datasets(catalog, metric.name),
        grain=Grain(group_by),
        facts_read=catalog.facts_read(metric),
        measurement_basis=metric.measurement_basis,
        units=metric.unit_kind,
        semantic_qualifiers=_semantic_qualifiers(
            _QualifierRequest(catalog, metric, source_timezone=single.source_timezone)
        ),
        source_timezone=single.source_timezone,
        restatement_window_days=single.restatement_window_days,
        suppression_columns=single.suppression_columns,
        partially_written_columns=single.partially_written_columns,
        not_returned_columns=single.not_returned_columns,
        typed_columns=single.typed_columns,
        # Only when there is money to denominate. Asking for `currency_code` from
        # `fct_product_page_views` would name a column that fact does not have.
        carried_columns=((CURRENCY_COLUMN,) if metric.unit_kind is UnitKind.CURRENCY else ()),
        currency_source=_currency_source(metric, single, measures),
        period=period,
        periodic=period is not None,
        roster=roster,
    )


def _currency_source(metric: Metric, shape: FactShape, measures: tuple[str, ...]) -> str:
    """Name the column a plan's money is labelled from, refusing measures of two currencies.

    A measure whose contract names its own currency column is labelled from it. Two measures of
    one plan (a difference, a same-fact ratio) must share one source: pack 5's DSP and sponsored
    window spends carry their own currencies, so no plan combines them, and none is built that
    would label one with the other's currency.

    On a fact with no `currency_code` column, a measure that names no currency column isn't
    money (pack 8's impressions beside its DSP spend), so it adds no source: a segment's CPM is
    labelled from `dsp_currency`, while its sales over its spend, each in its own currency, is
    still refused.
    """
    declared = dict(shape.currency_sources)
    defaulted = CURRENCY_COLUMN in shape.columns
    sources = {
        declared.get(measure, CURRENCY_COLUMN)
        for measure in measures
        if measure in declared or defaulted
    }
    if sources <= {CURRENCY_COLUMN}:
        return CURRENCY_COLUMN
    if len(sources) != 1 or (len(measures) != 1 and defaulted):
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} combines {', '.join(measures)}, and at least one carries its "
                "own currency column, so their amounts can't be combined in one currency."
            ),
        ).to_error()
    if metric.unit_kind is not UnitKind.CURRENCY:
        return CURRENCY_COLUMN
    (source,) = sources
    return source


def leaf_references(catalog: Catalog, metric: Metric) -> frozenset[tuple[str, str]]:
    """Every (fact, column) a metric's simple leaves read, through ratios and products."""
    if metric.kind is MetricKind.SIMPLE:
        return frozenset(references_in_definition(metric.definition))
    names = (
        *(name for name in (metric.numerator, metric.denominator) if name),
        *metric.operands,
    )
    found: set[tuple[str, str]] = set()
    for name in names:
        operand = catalog.metric(name)
        if operand is None:  # pragma: no cover — catalog construction rejects it
            raise CatalogError(f"{metric.name} has unknown operand {name}.")
        found.update(leaf_references(catalog, operand))
    return frozenset(found)


def _pack_columns(
    catalog: Catalog, metric: Metric, shapes: tuple[FactShape, ...]
) -> tuple[PackVersionedColumn, ...]:
    """Return the pack-versioned columns a metric's leaves read, from the shapes it plans over."""
    references = leaf_references(catalog, metric)
    return tuple(
        dict.fromkeys(
            column
            for shape in shapes
            for column in shape.pack_versioned_columns
            if (column.fact, column.column) in references
        )
    )


@dataclass(frozen=True, slots=True)
class _AdditivityCheck:
    """Everything the non-additivity rule reads, so the check takes one argument.

    A request object rather than a longer signature, per the hexagonal steering: the rule needs
    the dictionary to resolve non-additivity through a ratio's operands, the metric, and both
    halves of the resolved grain, and that is four things whichever way they are grouped.
    """

    catalog: Catalog
    metric: Metric
    request: PlanRequest
    shape: FactShape
    group_by: tuple[str, ...]
    exact_period: bool = False
    """The request reads one exact measured period, so nothing spans days (ADR-100)."""


def _outside_grain_set(grain_set: GrainSetShape | None) -> frozenset[SemanticAxis]:
    """Return the axes a pinned grain set's rows can't be rolled up across (ADR-099 point 2).

    Its own grain makes a row unique, and the grain check has already made the request group or
    filter all of it. A column outside that grain is stored NULL by option or follows from the
    grain (an ad product's channel), so nothing is rolled up across it.
    """
    if grain_set is None:
        return frozenset()
    return frozenset(
        axis for column, axis in _COLUMN_AXES.items() if column not in grain_set.grain_columns
    )


def _refuse_a_split_the_grain_set_lacks(
    metric: Metric, shape: FactShape, request: PlanRequest
) -> None:
    """Refuse grouping by a column the pinned grain set stores empty (D-1).

    Each grain set is its own extraction, so new buyers per ad product exist only at the grain set
    that split by ad product. The platform grain set stores the ad product NULL by option, and
    grouping its one row by it would describe a breakdown nobody measured.
    """
    grain_set = shape.grain_set_for(request.filters)
    names = {item.name for item in shape.grain_sets}
    if grain_set is None or len(names) < 2:  # noqa: PLR2004 — a choice needs two
        return
    lacking = tuple(
        column
        for name in request.grains
        if (column := GRAIN_COLUMNS[name]) in grain_set.not_applicable
    )
    if not lacking:
        return
    raise Refusal(
        requested=metric.name,
        reason=RefusalReason.GRAIN_NOT_SUPPORTED,
        explanation=(
            f"{metric.name} at grain set {grain_set.name} doesn't split by "
            f"{', '.join(lacking)}: that grain set stores it empty. Each grain set is its own "
            "extraction, so a figure is read only at the grain set that measured it, never "
            "derived from another."
        ),
        offer_instead=_grains_served_alone(metric, shape),
    ).to_error()


def _grains_served_alone(metric: Metric, shape: FactShape) -> tuple[str, ...]:
    """Return the metric's grains a request can name alone and be answered (D-1).

    A grain alone reads the grain set `served_grain_set` picks for it, so it's offered only when
    that grain set splits by it. A channel holds several ad products, so channel alone reads grain
    set platform, which stores it empty: offering it back would repeat the refused request.
    """
    offered: list[str] = []
    for name in metric.valid_grains:
        column = GRAIN_COLUMNS.get(name)
        if column is None:
            continue
        chosen = served_grain_set(shape.grain_sets, frozenset({column}))
        serving = next((item for item in shape.grain_sets if item.name == chosen), None)
        if serving is not None and column not in serving.not_applicable:
            offered.append(name)
    return tuple(offered)


def _refuse_aggregating_a_distinct_count(check: _AdditivityCheck) -> None:
    """Refuse a non-additive metric at any grain coarser than the one it was extracted at.

    **A stricter rule than ADR-024's, and the difference is the date.** That check treats
    `date_key` as accounted for by the window, which is right for a sum: a seven-day total is
    the seven daily totals added up. It is wrong for a distinct count. Monday's distinct
    shoppers plus Tuesday's is not two days' distinct shoppers, AMC has no approximate-distinct
    function so no sketch can be exported and merged, and the resulting figure overstates reach
    by however much the two days overlapped —
    [answerable-questions](../../../docs/answerable-questions.md) D-6.

    So a window spanning several days must group by date. A single-day window need not, because
    there is nothing to aggregate across.

    Non-additivity is resolved through the operands rather than read off the entry, so
    `avg_frequency` and `cost_per_reach` are caught by their denominator without declaring it
    themselves.

    Raised rather than silently degraded because the wrong answer here looks entirely normal:
    a reach figure is a plausible number whatever grain produced it, and nothing downstream can
    tell that it double-counted a shopper.
    """
    metric = check.metric
    request = check.request
    if not check.catalog.is_non_additive(metric):
        return

    grouped = frozenset(check.group_by)
    # The suppression discriminator is accounted for by the fold, and here the argument is
    # sharper than ADR-024's rather than merely the same. A grain holds at most one unsuppressed
    # row, so `SUM(CASE WHEN NOT flag …)` over a group sums exactly one distinct count — it
    # separates the two populations instead of adding them. That is why filtering the column was
    # always accepted, and folding it makes the identical claim. ADR-035.
    folded = frozenset(_folded_columns(check.shape.suppression_columns))
    grain_set = check.shape.grain_set_for(request.filters)
    absent = frozenset(() if grain_set is None else grain_set.absent_on(request.filters))
    ungrouped = tuple(
        column
        for column in check.shape.grain_for(request.filters)
        if column not in grouped
        and column not in request.filters
        and column not in folded
        and column not in absent
        and column != DATE_COLUMN
        and not (check.exact_period and column in {PERIOD_START_COLUMN, PERIOD_END_COLUMN})
    )
    spans_days = request.window.days > 1 and DATE_COLUMN not in grouped and not check.exact_period

    if not ungrouped and not spans_days:
        return

    problems = list(ungrouped)
    if spans_days:
        problems.append(f"{request.window.days} days of {DATE_COLUMN}")

    raise Refusal(
        requested=metric.name,
        reason=RefusalReason.GRAIN_NOT_SUPPORTED,
        explanation=(
            f"{metric.name} counts distinct users, so it cannot be aggregated across "
            f"{', '.join(problems)}. Two periods' distinct shoppers are not the sum of each "
            "period's, because the same shopper may appear in both, and the source publishes "
            "no sketch that could be merged to find out. A coarser figure needs a new "
            "extraction at that grain rather than an aggregation of this one."
        ),
        offer_instead=metric.valid_grains,
    ).to_error()


# A difference of two sums, which is the only multi-column expression a plan can express.
# Anchored at both ends so a definition with anything else in it — a third term, a product, a
# parenthesised group — does not match and is refused rather than partially understood.
_DIFFERENCE_OF_SUMS = re.compile(
    r"^sum\(\s*[a-z0-9_]+\.[a-z0-9_]+\s*\)\s*-\s*sum\(\s*[a-z0-9_]+\.[a-z0-9_]+\s*\)$"
)

_DIFFERENCE_TERMS = 2
"""How many fact references a difference of sums has. Named so the check reads as the rule."""


def _measures_of(catalog: Catalog, metric: Metric) -> tuple[str, tuple[str, ...], Combination]:
    """Resolve a metric to the fact it reads, the columns to aggregate, and how to combine them.

    Three of the four plan kinds land here, and each is read from the declaration rather than
    guessed: one aggregate for a plain measure, two subtracted for a difference of sums, two
    divided for a ratio whose operands share a fact.

    The fourth is refused. A ratio spanning two facts is a drill-across — aggregate each side
    separately and join the results — and joining needs a rule for a row present on one side
    only, which is a product decision rather than a planning detail.
    """
    if metric.kind is MetricKind.RATIO:
        return _ratio_measures(catalog, metric)
    if metric.kind is MetricKind.PRODUCT:
        # A product's honest figure is derived at publish time from its already-measured
        # operands (the baked-artifact path); a live query for it would have to multiply
        # aggregates this planner never composes. Refused with the operands named, so a
        # caller can measure those instead.
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} is a declared product of operands, computed at publish time "
                "from its measured operands rather than by a live query."
            ),
            offer_instead=metric.operands,
        ).to_error()
    return _simple_measures(metric)


def _simple_measures(metric: Metric) -> tuple[str, tuple[str, ...], Combination]:
    """Read a simple metric's declared expression into aggregates.

    One reference is the ordinary case. Two are accepted only in the difference-of-sums shape,
    which exists because Amazon publishes a total and a part and the useful figure is the
    remainder — `attributed_revenue_halo_14d` rests on the documented identity
    `total_product_sales = product_sales + brand_halo_product_sales`.

    Anything else is refused rather than approximated. A definition this cannot parse is a
    declaration the platform does not know how to compute, and computing some of it would
    produce a number nobody declared.
    """
    references = references_in_definition(metric.definition)
    if len(references) == 1:
        fact, column = references[0]
        return fact, (column,), Combination.SINGLE

    normalised = " ".join(metric.definition.split())
    if len(references) == _DIFFERENCE_TERMS and _DIFFERENCE_OF_SUMS.match(normalised):
        (first_fact, first), (second_fact, second) = references
        if first_fact == second_fact:
            return first_fact, (first, second), Combination.DIFFERENCE

    raise Refusal(
        requested=metric.name,
        reason=RefusalReason.NOT_YET_COMPUTABLE,
        explanation=(
            f"{metric.name} declares an expression over {len(references)} fact reference(s) "
            "that this planner cannot compute. A single aggregate and a difference of two sums "
            "over one fact are expressible; anything else would mean computing part of a "
            "declaration and presenting it as the whole."
        ),
    ).to_error()


def _operand_sides(catalog: Catalog, metric: Metric) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Resolve both of a ratio's operands to the fact and columns each aggregates."""
    sides: list[tuple[str, tuple[str, ...]]] = []
    for operand in (metric.numerator, metric.denominator):
        referenced = catalog.metric(operand)
        if referenced is None:  # pragma: no cover — the dictionary gate refuses this
            raise Refusal(
                requested=metric.name,
                reason=RefusalReason.NOT_YET_COMPUTABLE,
                explanation=(
                    f"{metric.name} names {operand}, which is not a declared metric, so its "
                    "fact cannot be resolved."
                ),
            ).to_error()
        fact, columns, _ = _simple_measures(referenced)
        sides.append((fact, columns))
    return tuple(sides)


def _leaf_source_datasets(catalog: Catalog, metric_name: str) -> tuple[str, ...]:
    metric = catalog.metric(metric_name)
    if metric is None:
        raise CatalogError(f"unknown metric {metric_name!r} while resolving publication sources.")
    if metric.kind is MetricKind.SIMPLE:
        return metric.source_datasets
    found: list[str] = []
    for operand in (metric.numerator, metric.denominator):
        for dataset in _leaf_source_datasets(catalog, operand):
            if dataset not in found:
                found.append(dataset)
    return tuple(found)


def _drill_across(
    catalog: Catalog, metric: Metric
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Return both sides of a cross-fact ratio, or None when this is not one.

    The test is only whether the operands' facts differ. Everything else about the metric is
    already settled by the dictionary, so this is the one question that decides which plan shape
    a ratio needs.
    """
    if metric.kind is not MetricKind.RATIO:
        return None

    (numerator_fact, numerator_columns), (denominator_fact, denominator_columns) = _operand_sides(
        catalog, metric
    )
    if numerator_fact == denominator_fact:
        return None

    if len(numerator_columns) != 1 or len(denominator_columns) != 1:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} spans two facts and has an operand that is itself a multi-column "
                "expression, so neither side reduces to a single aggregate to join on."
            ),
        ).to_error()
    return (numerator_fact, numerator_columns[0]), (denominator_fact, denominator_columns[0])


def _ratio_measures(catalog: Catalog, metric: Metric) -> tuple[str, tuple[str, ...], Combination]:
    """Resolve a ratio whose operands share one fact into two aggregates over it.

    Reached only after `_drill_across` has established the facts are the same, so one query can
    aggregate both and divide — and dividing the *sums* is the only correct order, because the
    mean of per-row ratios is not the ratio of the sums.
    """
    (numerator_fact, numerator_columns), (_, denominator_columns) = _operand_sides(catalog, metric)
    if len(numerator_columns) != 1 or len(denominator_columns) != 1:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} has an operand that is itself a multi-column expression, so the "
                "quotient would need more than two aggregates."
            ),
        ).to_error()
    return numerator_fact, (numerator_columns[0], denominator_columns[0]), Combination.QUOTIENT


@dataclass(frozen=True, slots=True)
class _DrillRequest:
    """Everything planning a drill-across reads, so the function takes one argument.

    A request object rather than a longer signature, per the hexagonal steering. Four things are
    genuinely needed — the dictionary, the question, both shapes, and the resolved sides — and no
    grouping of them is shorter.
    """

    catalog: Catalog
    request: PlanRequest
    shapes: FactShapes | None
    """None when the caller supplied one shape for a metric that needs two. Refused, not filled
    in: without the second fact's grain there is nothing to account for it against."""

    across: tuple[tuple[str, str], tuple[str, str]]
    period: CalendarPeriod | None = None
    """The exact period when either side is periodic (ADR-100)."""


def _additive_rollup_columns(catalog: Catalog, metric_name: str) -> frozenset[str]:
    metric = catalog.metric(metric_name)
    if metric is None:
        return frozenset()
    if metric.kind is MetricKind.RATIO:
        numerator = _additive_rollup_columns(catalog, metric.numerator)
        denominator = _additive_rollup_columns(catalog, metric.denominator)
        return numerator & denominator
    if metric.aggregation_policy is None:
        return frozenset()
    additive_axes = {
        axis
        for axis, policy in metric.aggregation_policy.axes.items()
        if policy.aggregation is AxisBehavior.ADDITIVE
    }
    return frozenset(
        column for name, column in GRAIN_COLUMNS.items() if _GRAIN_AXES[name] in additive_axes
    )


def _plan_drill_across(drill: _DrillRequest) -> QueryPlan:
    """Plan a ratio across two facts, aggregating each side and joining on their shared grain.

    Refuses rather than guessing in three places, and each one is a number that would otherwise
    look entirely normal:

    - **No second shape supplied.** The caller has not said what the denominator's fact looks
      like, so its grain cannot be accounted for. Answering anyway would sum across a column
      nobody pinned.
    - **A grain the two facts do not share.** ROAS by product is the case: `fct_ad_exposure` has
      no product dimension, so a product grain divides product-level revenue by spend broadcast
      across products.
    - **A grain column of either fact left unaccounted.** ADR-024 runs per side, because the two
      facts have different grain columns and neither one's filter list is valid for the other.
    """
    catalog = drill.catalog
    request = drill.request
    metric = catalog.metric(request.metric_name)
    if metric is None:  # pragma: no cover — plan() resolved it before calling this
        raise CatalogError(f"{request.metric_name} vanished between resolution and planning.")
    (numerator_fact, numerator_column), (denominator_fact, denominator_column) = drill.across

    if drill.shapes is None:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} divides a figure from {numerator_fact} by one from "
                f"{denominator_fact}. That is a drill-across — each side aggregated separately "
                "and the results joined on the dimensions they share — and this request supplied "
                f"no description of {denominator_fact}, so its grain cannot be accounted for."
            ),
            offer_instead=(metric.numerator, metric.denominator),
        ).to_error()

    numerator_shape = drill.shapes.numerator
    denominator_shape = drill.shapes.denominator
    if numerator_shape.name != numerator_fact or denominator_shape.name != denominator_fact:
        raise ValidationError(
            code=UNFILTERABLE_COLUMN,
            description=(
                f"{metric.name} reads {numerator_fact} and {denominator_fact}, but the shapes "
                f"supplied describe {numerator_shape.name} and {denominator_shape.name}."
            ),
            context={"metric": metric.name},
        )

    group_by = tuple(GRAIN_COLUMNS[name] for name in request.grains)
    shared = frozenset(numerator_shape.columns) & frozenset(denominator_shape.columns)
    unshared = tuple(column for column in group_by if column not in shared)
    if unshared:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.GRAIN_NOT_SUPPORTED,
            explanation=(
                f"{metric.name} joins {numerator_fact} to {denominator_fact}, which share no "
                f"{', '.join(unshared)}. A ratio is valid only where both operands carry the "
                "dimension, and dividing by a figure broadcast across it produces a number that "
                "is computable, plausible and wrong."
            ),
            offer_instead=metric.valid_grains,
        ).to_error()

    sides_periodic = (
        _sides_periodic(catalog, metric) if drill.period is not None else (False, False)
    )
    # A periodic side's `time_grain` and `grain_set` select its exact period and grain set; a
    # daily side has neither column (D-6). They are structural there, not a population filter.
    structural: frozenset[str] = (
        frozenset({PERIOD_GRAIN_SELECTOR, GRAIN_SET_COLUMN}) if any(sides_periodic) else frozenset()
    )
    declared_asymmetry = {
        *(key for key, _value in metric.requires),
        *(key for key, _values in metric.required_selectors),
    }
    qualified_asymmetry = tuple(
        column
        for column in request.filters
        if (column in numerator_shape.columns) != (column in denominator_shape.columns)
        and column in declared_asymmetry
        and column not in structural
    )
    one_sided = sorted(
        column
        for column in request.filters
        if (column in numerator_shape.columns) != (column in denominator_shape.columns)
        and column not in declared_asymmetry
        and column not in structural
    )
    if one_sided:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.GRAIN_NOT_SUPPORTED,
            explanation=(
                f"{metric.name} cannot apply {', '.join(one_sided)} to only one operand; "
                "that would divide different populations."
            ),
        ).to_error()

    carries_currency = _result_carries_currency(catalog, metric)
    primary_carried = (
        (CURRENCY_COLUMN,)
        if carries_currency and CURRENCY_COLUMN in numerator_shape.columns
        else ()
    )
    joined_carried = (
        (CURRENCY_COLUMN,)
        if carries_currency and CURRENCY_COLUMN in denominator_shape.columns
        else ()
    )
    output_carried = tuple(dict.fromkeys((*primary_carried, *joined_carried)))
    sides = tuple(
        _side_filters(
            _SideRequest(
                shape=shape,
                request=request,
                group_by=group_by,
                metric_name=metric.name,
                additive_rollups=_additive_rollup_columns(catalog, operand_name),
                carried_columns=carried,
                periodic=side_periodic,
            )
        )
        for shape, operand_name, carried, side_periodic in zip(
            (numerator_shape, denominator_shape),
            (metric.numerator, metric.denominator),
            (primary_carried, joined_carried),
            sides_periodic,
            strict=True,
        )
    )
    periodic_shape = (
        denominator_shape if sides_periodic[1] else numerator_shape if sides_periodic[0] else None
    )
    grain_set = None
    if periodic_shape is not None:
        _refuse_a_split_the_grain_set_lacks(metric, periodic_shape, request)
        grain_set = periodic_shape.grain_set_for(request.filters)
    _validate_semantic_axes(catalog, metric, request, grain_set)
    roster = require_roster(
        (
            None if grain_set is None else grain_set.roster_for(group_by, request.filters),
            request.roster,
            request.window,
            request.filters,
        )
    )

    return QueryPlan(
        metric=metric,
        fact=numerator_fact,
        measures=(numerator_column,),
        combination=Combination.QUOTIENT,
        multiplier=metric.multiplier,
        join=JoinSide(
            fact=denominator_fact,
            measure=denominator_column,
            filters=sides[1],
            source_datasets=_leaf_source_datasets(catalog, metric.denominator),
            carried_columns=joined_carried,
            suppression_columns=denominator_shape.suppression_columns,
            periodic=sides_periodic[1],
            typed_columns=denominator_shape.typed_columns,
        ),
        group_by=group_by,
        window=request.window,
        filters=sides[0],
        source_datasets=_leaf_source_datasets(catalog, metric.numerator),
        grain=Grain(group_by),
        facts_read=catalog.facts_read(metric),
        measurement_basis=metric.measurement_basis,
        units=metric.unit_kind,
        semantic_qualifiers=(
            *_semantic_qualifiers(_QualifierRequest(catalog, metric, drill.shapes)),
            *(
                CompatibilityDifference(
                    CompatibilityField.SOURCE_OPTION,
                    request.filters[column] if column in numerator_shape.columns else None,
                    request.filters[column] if column in denominator_shape.columns else None,
                )
                for column in qualified_asymmetry
            ),
        ),
        # The numerator's zone and window. Both sides are stated in the answer when they differ —
        # a UTC advertising day beside a Pacific retail day is the caveat answerable-questions §5
        # makes mandatory for a cross-source comparison.
        source_timezone=numerator_shape.source_timezone,
        restatement_window_days=max(
            numerator_shape.restatement_window_days, denominator_shape.restatement_window_days
        ),
        suppression_columns=numerator_shape.suppression_columns,
        # Both sides' partial columns, because either operand may be one. `ad_spend` sits in
        # `fct_ad_exposure`, which four contracts write, so the numerator of `cost_per_purchase`
        # is partially written while its denominator is not.
        partially_written_columns=_merged_partials(numerator_shape, denominator_shape),
        not_returned_columns=tuple(
            dict.fromkeys(
                (*numerator_shape.not_returned_columns, *denominator_shape.not_returned_columns)
            )
        ),
        typed_columns=numerator_shape.typed_columns,
        carried_columns=output_carried,
        primary_carried_columns=primary_carried,
        period=drill.period,
        periodic=sides_periodic[0],
        roster=roster,
    )


def _sides_periodic(catalog: Catalog, metric: Metric) -> tuple[bool, bool]:
    """Whether each operand of a cross-fact ratio reads a periodic fact."""

    def periodic(name: str) -> bool:
        operand = catalog.metric(name)
        return operand is not None and bool(periodic_leaves(catalog, operand))

    return periodic(metric.numerator), periodic(metric.denominator)


def _result_carries_currency(catalog: Catalog, metric: Metric) -> bool:
    """Carry currency for money and for ratios whose two operands are monetary."""
    if metric.unit_kind is UnitKind.CURRENCY:
        return True
    if metric.kind is not MetricKind.RATIO:
        return False
    numerator = catalog.metric(metric.numerator)
    denominator = catalog.metric(metric.denominator)
    return (
        numerator is not None
        and denominator is not None
        and numerator.unit_kind is UnitKind.CURRENCY
        and denominator.unit_kind is UnitKind.CURRENCY
    )


def _merged_partials(numerator: FactShape, denominator: FactShape) -> tuple[str, ...]:
    """Union both sides' partially written columns, keeping order and dropping duplicates."""
    merged: list[str] = []
    for shape in (numerator, denominator):
        for column in shape.partially_written_columns:
            if column not in merged:
                merged.append(column)
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class _SideRequest:
    """One side of a drill-across and the question being asked of it."""

    shape: FactShape
    request: PlanRequest
    group_by: tuple[str, ...]
    metric_name: str
    additive_rollups: frozenset[str] = frozenset()
    carried_columns: tuple[str, ...] = ()
    periodic: bool = False


def _side_filters(side: _SideRequest) -> Mapping[str, str]:
    """Take the filters that belong to one side's fact, and refuse an unaccounted grain column.

    The caller supplies one flat filter map and it is split here by which fact declares each
    column, because a single map is what a question naturally has — nobody asks about
    `ad_product_key` "on the exposure side". A key neither fact declares is already refused by
    `plan()`; a key one fact declares and the other does not simply does not apply to the other.
    """
    shape = side.shape
    filters = {
        declared: side.request.filters[declared]
        for declared in shape.columns
        if declared in side.request.filters
    }
    accounted = frozenset(
        {
            *side.group_by,
            *filters,
            *side.carried_columns,
            DATE_COLUMN,
            *_folded_columns(shape.suppression_columns),
            *((PERIOD_START_COLUMN, PERIOD_END_COLUMN) if side.periodic else ()),
        }
    )
    unaccounted = tuple(
        column
        for column in shape.grain_for(filters)
        if column not in accounted and column not in side.additive_rollups
    )
    if unaccounted:
        raise Refusal(
            requested=side.metric_name,
            reason=RefusalReason.GRAIN_NOT_SUPPORTED,
            explanation=(
                f"{side.metric_name} over {shape.name} would sum across "
                f"{', '.join(unaccounted)}, which that fact's grain says makes a row distinct. "
                "Each side of a ratio across two facts accounts for its own grain, because the "
                "two facts do not have the same one. Group by each column or filter it."
            ),
        ).to_error()
    return filters
