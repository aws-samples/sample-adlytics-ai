# SPDX-License-Identifier: MIT-0
"""What a data contract declares.

A contract is the *adapter* from one source dataset into the canonical model — not a
description of a source. Facts about sources live in docs/sources/*.md; what a contract
does with them lives here. Owned by docs/capabilities/data-layer/low-level-design.md §6 and the
data-contract-definition steering.

Pure domain: no YAML, no file system, no AWS. Loading a contract off disk is an
adapter's job (`adapters/jobs/contract_validation`), so these types can be constructed
in a test without touching anything.

Structural invariants that cannot be anything other than wrong live in `__post_init__`
here. Everything requiring judgement, cross-referencing the AMC column registry, or
comparing two declarations against each other is a rule in `validation.py` — because a
contract that fails those should produce a *reported finding a human reads*, not an
exception that stops at the first problem. Naming every gap at once is the same reason
`getConfig` in infra does it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from domain.shared_kernel.provenance import MeasurementBasis

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

FACT_PREFIX = "fct_"
DIMENSION_PREFIX = "dim_"
TABLE_PREFIXES = (FACT_PREFIX, DIMENSION_PREFIX)
"""The canonical model's two table kinds, as their name prefixes.

Load-bearing rather than stylistic: the prefix is what decides whether a table gets the
conformed provenance columns, so it is validated at construction instead of trusted.
Naming owned by docs/canonical-model.md.
"""

CELL_STATE_COLUMN = "cell_state"
"""Whether a shopper-aggregate cell came back (`returned`) or not (`not_returned`), ADR-099."""

VALID_FROM_COLUMN = "valid_from"
"""An SCD2 dimension's version start; a dimension writing it folds rows into a history."""


class ContractError(Exception):
    """A contract is structurally malformed. Raised at construction."""


class SourceFormat(StrEnum):
    """Which reader parses the landed object.

    One of exactly two things that vary per source, and it is declared as data rather
    than written as code — see docs/capabilities/data-layer/architecture.md §4. Adding a
    channel adds a value here and a field-mapping table, never a new pipeline.
    """

    CSV = "csv"
    """AMC workflow output."""

    JSON_DUAL_ARRAY = "json_dual_array"
    """Vendor Central reports: two sibling arrays at two different grains per document."""

    JSONL = "jsonl"
    """Vendor Data Kiosk."""


class NullMeaning(StrEnum):
    """Why a value is absent. Distinct facts that must never be collapsed.

    Collapsing them is how a privacy withholding becomes a zero. Every nullable column
    declares which one applies, and the curated row carries a status column saying which
    — see docs/concepts/provenance-chain.md. The meanings are listed once, here and in
    docs/capabilities/data-layer/README.md.
    """

    SUPPRESSED = "suppressed"
    """A privacy threshold withheld it. Not zero, and not unknown."""

    NOT_APPLICABLE_UNDER_OPTIONS = "not_applicable_under_options"
    """The source's own options, or the contract's declared grain set, exclude it.

    Vendor SOURCING or FRESH; or a media column that a shopper-aggregate contract's grain set
    doesn't split by, such as `campaign_key` on a platform-grain contract (ADR-099 point 2); or
    a column a governed query pack adds at a later version, which a delivery produced by an
    earlier version of that pack doesn't carry (`FieldMapping.present_from_pack_version`).
    """

    NOT_YET_AVAILABLE = "not_yet_available"
    """A partial period. It will be populated later."""

    UNSUPPORTED_BY_CHANNEL = "unsupported_by_channel"
    """This channel has no such concept. Emphatically not zero."""

    NOT_RETURNED = "not_returned"
    """An expected cell of a declared closed enumeration came back with no row (ADR-099 pt 8).

    The cell is absent, not zero: too few shoppers, or none. A suppressed cell and an empty one
    both come back with no row, so this claims neither `suppressed` nor a measured zero. It
    renders "not available", and a sum across a disjoint axis with such a cell is refused.
    """


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """One source field becoming one canonical column.

    `source_path` is the exact path, not a name: `salesByAsin[].shippedRevenue.amount`
    rather than `shippedRevenue`. A name is ambiguous across the two grains a vendor
    document carries.
    """

    source_path: str = ""
    target_column: str = ""

    scale_divisor: int | None = None
    """Set on every monetary column. The divisor is a property of the COLUMN.

    Never of the table or the platform: `dsp_impressions` mixes millicents and
    microcents, so a per-table divisor makes every fee figure wrong by exactly 1000x in
    the direction that makes fees look negligible — while fee-inclusive ROAS still
    reconciles, so the headline looks right and the decomposition is garbage.
    """

    currency_column: str | None = None
    """The canonical column carrying this amount's currency. Required for money."""

    null_meaning: NullMeaning | None = None
    """Required when the column is nullable. Which `NullMeaning` applies."""

    nullable: bool = False

    non_additive: bool = False
    """True for a distinct count. Reach is the case that matters.

    Live AMC evidence shows discriminator mode can preserve duplicate fully-nulled grain rows
    rather than merge a non-additive distinct count. Keeping duplicates violates the declared key;
    folding them would sum distinct counts. The load therefore refuses any suppressed delivery
    carrying a non-additive measure.
    """

    derived_from: tuple[str, ...] = ()
    """AMC columns the extraction SQL reads to compute this value, when it is not a
    straight column mapping.

    Reach is why this exists. `COUNT(DISTINCT user_id)` cannot be expressed as a field
    mapping, because `user_id` is `VERY_HIGH` and may never appear in output — it is legal
    only inside a CTE. Naming the inputs keeps them verifiable against the registry while
    letting the *output* column be something no registry entry describes.

    Two consequences follow, both deliberate. A derived input may be `VERY_HIGH`, since
    CTE use is exactly what it is for. And a derived input does **not** escalate the
    query's threshold, because the bar is set by the columns in the final output and a CTE
    column is not one of them — which is the only reason reach is obtainable at all.

    `source_path` carries the expression for a human to read; this carries the bare column
    names for the gate to check.
    """

    target_type: str | None = None
    """The curated column's type, when no source states it.

    Left absent for an AMC source column, because Amazon publishes the type and
    `contracts/shared/amc-columns.yaml` records it `[verified]` — re-declaring it here would
    create a second copy that can drift from the registry. Required for a vendor field, a
    derived value, a literal, or an extraction-option column, none of which any source
    types for us.

    Also left absent on a monetary column whatever the source: the divisor turns it into a
    decimal, so its source type is actively the wrong answer. See
    `domain/data_contracts/schema.py`.
    """

    value_domain: tuple[str, ...] = ()
    """The closed set of values this column can hold, when the source documents one.

    Declared for one reason: on a fact several contracts write, a load must know **which rows
    it owns**. `fct_ad_exposure` is written by four contracts, two of which supply `impressions`
    and `spend`. They are kept apart by `channel_key`, and a withdraw that cannot express the
    values its own `channel_key` takes nulls the other contract's figures across the partition
    — see [ADR-033](../../../docs/decisions/adr-033-a-load-owns-columns-not-a-partition.md).

    A literal already states this for one value. This states it for a column *read* from the
    source: `sponsored_ads_traffic.ad_product_type` is documented as exactly four values, so a
    contract mapping it can declare that and be provably disjoint from the DSP contracts'
    stamped `amazon_dsp`.

    Only worth declaring on a grain column, and only when the source documentation is explicit.
    Left empty means unbounded, which is honest for `campaign_key` — no source enumerates the
    campaigns. An empty domain is not a defect; a *wrong* one silently narrows a withdraw.
    """

    literal_value: str | None = None
    """A constant stamped during mapping, with no source column behind it at all.

    `channel_key` on a DSP contract is the case. DSP tables carry no `ad_product_type`
    column — AMC's convention is that a NULL `ad_product_type` on the attributed-events
    tables *means* DSP — so there is nothing to map and the channel is knowledge we hold,
    not data we received. Same shape as `measurement_basis`, which is a contract-level
    literal for the same reason.

    Declared rather than left to the pipeline so the value is reviewable and the gate can
    see the column is produced. A literal takes no `source_path`, because inventing one
    would make a constant look like a field we read.
    """

    assigned_by_load: bool = False
    """True when the load computes this column, and neither the source nor mapping can.

    `valid_from` and `valid_to` on an SCD2 dimension are the case. Data Kiosk reports current
    state with no validity window, so the window is something we *construct by comparing
    loads* — the merge decides it by finding the row this one supersedes.

    Distinct from a literal, and the distinction was found by trying to read a contract. Both
    have no source column, so both were declared as literals; but a literal has a value the
    mapping can stamp, while these had prose describing who would fill them in later. The
    mapper duly stamped `(load date, assigned by the SCD2 merge)` into a `timestamp` column
    and the row model rejected it. Naming the difference means the mapper skips them and the
    merge supplies them, rather than a reader having to know which literals are real.

    A `target_type` is required, since no source states one, and no `source_path` is legal —
    the same reasons as for a literal.
    """

    from_extraction_option: bool = False
    """True when AMC adds this column to the output because the workflow asked for it.

    The three suppression columns are the case. `filteredMetricsDiscriminatorColumn`,
    `filteredReasonColumn` and `distinctUserCountColumn` are workflow *options*, not
    columns on any table — AMC synthesises them into the delivered output, and the names
    are ones we choose. So they can never resolve against the column registry, and a
    contract that declared them as source columns would be asserting they exist in a
    schema where they do not.

    Distinct from `derived_from`: that is a value our SQL computes from named columns, this
    is a value the platform supplies because of configuration. Both bypass the registry,
    for different reasons.
    """

    present_from_pack_version: int | None = None
    """The governed query pack version that first outputs this column, when it isn't the first.

    A pack that adds a column keeps loading deliveries its earlier version produced: those read
    NULL here, which is why the column must be nullable with `not_applicable_under_options` (the
    pack version is the option). A delivery produced at this version or later must carry a value
    on every row, which the load asserts (`check_pack_version_columns`); the generated DQ can't
    say it, because it can't see the delivering pack.
    """

    served_where_published: bool = False
    """True when this writer's column is served only on days its contract has published.

    For a writer that joined a fact whose rows it now owns had already landed without it: DSP
    reach (`amc-dsp-reach`, plan D6) writes `reach_users` on rows the three DSP delivery contracts
    have written since before it existed. Those older days carry no reach, so a request that pins
    the rows this contract owns (`DataContract.owned_grain_values`) is planned only over days a
    publication of this contract covers, and refused naming the first covered day otherwise
    (`domain.measurement.pack_coverage`). A request that doesn't pin them is unchanged.

    Only on a non-additive column: an additive one already reads its absence as no contribution.
    """

    def __post_init__(self) -> None:
        # Exactly one provenance. Four ways a canonical column comes to exist, and
        # conflating any two of them hides where a value actually came from.
        markers = sum(
            (
                bool(self.derived_from),
                bool(self.from_extraction_option),
                self.literal_value is not None,
                self.assigned_by_load,
            )
        )
        if markers > 1:
            raise ContractError(
                f"{self.target_column} declares more than one provenance. A column is "
                "read from a source, derived by the extraction, supplied by an extraction "
                "option, stamped as a literal, or assigned by the load — exactly one."
            )
        if self.literal_value is not None and self.source_path:
            raise ContractError(
                f"{self.target_column} is a literal and must not declare a source_path; "
                "a constant is not a field we read."
            )
        if self.assigned_by_load:
            if self.source_path:
                raise ContractError(
                    f"{self.target_column} is assigned by the load and must not declare a "
                    "source_path; nothing reads it from the delivered file."
                )
            if self.target_type is None:
                raise ContractError(
                    f"{self.target_column} is assigned by the load, so no source states its "
                    "type and target_type is required."
                )
        elif self.literal_value is None and not self.source_path:
            raise ContractError(
                f"{self.target_column or 'a field'} declares neither a source_path, a "
                "literal_value, nor assignment by the load, so nothing says where its value "
                "comes from."
            )
        if not self.target_column:
            raise ContractError(f"target_column is required for {self.source_path}.")
        if self.nullable and self.null_meaning is None:
            raise ContractError(
                f"{self.target_column} is nullable and must declare which meaning of null "
                "applies. Absent is not one of them."
            )
        if self.present_from_pack_version is not None and (
            self.present_from_pack_version < 2  # noqa: PLR2004 — version 1 is every pack's first
            or not self.nullable
            or self.null_meaning is not NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS
            or not self.resolves_against_registry
        ):
            raise ContractError(
                f"{self.target_column} declares present_from_pack_version, so it must be a "
                "source column added at pack version 2 or later, nullable with "
                "not_applicable_under_options: an earlier pack's delivery doesn't carry it."
            )
        if self.served_where_published and not self.non_additive:
            raise ContractError(
                f"{self.target_column} declares served_where_published, which only a non_additive "
                "column takes: an additive column already reads a missing writer as no "
                "contribution."
            )
        if self.scale_divisor is not None and self.scale_divisor < 1:
            raise ContractError(
                f"{self.target_column} has scale_divisor {self.scale_divisor}; "
                "a divisor is at least 1, where 1 means the value is already in "
                "local currency."
            )

    @property
    def monetary(self) -> bool:
        """Report whether this column carries an amount of money."""
        return self.scale_divisor is not None

    @property
    def is_derived(self) -> bool:
        """Whether the extraction SQL computes this rather than mapping a column."""
        return bool(self.derived_from)

    @property
    def is_literal(self) -> bool:
        """Whether this value is stamped rather than read from anywhere."""
        return self.literal_value is not None

    @property
    def delivered_name(self) -> str:
        """Give the column name the delivered file uses for this field.

        A derived value or an extraction-option column has no source name, so the convention is
        that the extraction aliases it to the canonical name — the same convention the fixture
        generator writes and docs/capabilities/data-layer/low-level-design.md §6 (Delivered column
        names) records. For a query pack, this is the name its SQL outputs.
        """
        if self.is_derived or self.from_extraction_option:
            return self.target_column
        return self.source_path.rsplit(".", maxsplit=1)[-1].replace("[]", "")

    @property
    def has_no_delivered_value(self) -> bool:
        """Whether the delivered file carries nothing for this column.

        True for a literal and for a load-assigned column. Both are produced by us rather than
        read, so neither appears in a fixture and neither is looked up when mapping.
        """
        return self.is_literal or self.assigned_by_load

    @property
    def resolves_against_registry(self) -> bool:
        """Whether this field's source should be looked up as a real table column."""
        return not (
            self.is_derived
            or self.from_extraction_option
            or self.is_literal
            or self.assigned_by_load
        )


@dataclass(frozen=True, slots=True)
class SuppressionColumns:
    """The three AMC output columns that make a privacy withholding visible.

    Declaring them is what makes AMC's nulling visible: without the discriminator, offending
    values are nulled with no flag; with it, identical-after-nulling rows are re-aggregated and
    flagged. The distinction is between unknowable and explicit suppression evidence.
    """

    discriminator_column: str
    reason_column: str
    distinct_user_count_column: str

    def __post_init__(self) -> None:
        for label, value in (
            ("discriminator_column", self.discriminator_column),
            ("reason_column", self.reason_column),
            ("distinct_user_count_column", self.distinct_user_count_column),
        ):
            if not value:
                raise ContractError(f"SuppressionColumns.{label} cannot be empty.")

    @property
    def all_columns(self) -> tuple[str, ...]:
        """The three column names, for checking they reach the target schema."""
        return (
            self.discriminator_column,
            self.reason_column,
            self.distinct_user_count_column,
        )


@dataclass(frozen=True, slots=True)
class ExpectedAxis:
    """One closed column of a declared expected-cell set, and the values it must return.

    `from_delivery` declares the delivery's own marketplace as the column's one value (pack 1's
    single platform cell), which no contract can name because the population is the
    registration's, not the contract's.
    """

    column: str
    values: tuple[str, ...] = ()
    from_delivery: bool = False

    def __post_init__(self) -> None:
        if not self.column.strip():
            raise ContractError("an expected-cell axis needs a column.")
        if self.from_delivery == bool(self.values):
            raise ContractError(
                f"expected cells: {self.column} declares either its values or `from: delivery`, "
                "exactly one."
            )
        if len(set(self.values)) != len(self.values) or any(not v.strip() for v in self.values):
            raise ContractError(f"expected cells: {self.column} needs unique, nonblank values.")


class CohortGrain(StrEnum):
    """The calendar grain a cohort triangle groups shoppers by (ADR-104; plan D9, D11)."""

    MONTH = "month"
    QUARTER = "quarter"


@dataclass(frozen=True, slots=True)
class CohortTriangle:
    """A cohort vintage's triangle of expected cells (design §6.6; Retention LLD).

    For as-of month *a*, monthly cells are every cohort month *m* in the vintage's 13 months and
    every follow-up month *k* from 1 to `follow_ups` with *m* + *k* <= *a*; quarterly cells are
    every calendar cohort quarter wholly inside the vintage and every follow-up quarter *j* from
    1 to `follow_ups` that has ended by the end of *a*. `column` holds the cohort's first day and
    `offset` the follow-up count. The cells depend on the vintage, so they are enumerated from
    the delivery's own period (`domain.shopper_aggregates.cohorts`), never typed here.
    """

    column: str
    offset: str
    grain: CohortGrain
    follow_ups: int

    def __post_init__(self) -> None:
        if not self.column.strip() or not self.offset.strip() or self.column == self.offset:
            raise ContractError("a cohort triangle names two distinct columns, cohort and offset.")
        if self.follow_ups < 1:
            raise ContractError("a cohort triangle needs at least one follow-up period.")


@dataclass(frozen=True, slots=True)
class ExpectedCellSet:
    """One product of closed axes a delivery must fill (ADR-099 point 8; design §4.5, plan D8).

    `slices` are the axes loss is graded over (packs 5 to 7: `identity_space`); `cells` the axes
    every slice must fill; `open_slices` the columns known only from the rows that came back (a
    roster-graded grain set: pack 2's ad product). `measures` names the distinct counts a
    returned row of this set must carry; empty means every non-additive measure, and a set that
    carries only some (a touch-only branch) names them. `serves` names every measure, distinct
    or additive, that serving reads from this set's rows; empty means every measure. A touch-only
    branch names its own, so a figure is read from the branch whose rows carry it (plan D9).
    """

    cells: tuple[ExpectedAxis, ...]
    slices: tuple[ExpectedAxis, ...] = ()
    open_slices: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    serves: tuple[str, ...] = ()
    cohorts: CohortTriangle | None = None
    """A cohort vintage's triangle (packs 14-15): its cohorts may be absent entirely, and an
    absent cohort is reported and left out of the expected cells (design §6.6, DS4-3)."""

    def __post_init__(self) -> None:
        if not self.cells:
            raise ContractError("an expected-cell set needs at least one cell axis.")
        columns = self.columns
        if len(set(columns)) != len(columns):
            raise ContractError("an expected-cell set names a column twice.")

    @property
    def columns(self) -> tuple[str, ...]:
        """Every column this set keys a cell on."""
        return (
            *(axis.column for axis in self.slices),
            *self.open_slices,
            *(() if self.cohorts is None else (self.cohorts.column, self.cohorts.offset)),
            *(axis.column for axis in self.cells),
        )


@dataclass(frozen=True, slots=True)
class ExpectedCells:
    """The cells a shopper-aggregate delivery must return, declared in its contract (plan D8).

    `sets` is the base pack's declaration. `variants` holds a declared pack variant's own sets
    (plan D9), keyed by the variant id its registration names: pack 5's touch-only variant
    returns exposure rows for both identity spaces and outcome rows for `account` only, which is
    two sets rather than one product.
    """

    sets: tuple[ExpectedCellSet, ...]
    variants: tuple[tuple[str, tuple[ExpectedCellSet, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not self.sets:
            raise ContractError("expected cells need at least one set.")
        names = tuple(name for name, _sets in self.variants)
        if len(set(names)) != len(names) or any(not sets for _name, sets in self.variants):
            raise ContractError("expected-cell variants need unique ids and at least one set.")

    def for_variant(self, variant: str | None) -> tuple[ExpectedCellSet, ...]:
        """Return the sets a delivery of `variant` (None for the base pack) must fill."""
        if variant is None:
            return self.sets
        for name, sets in self.variants:
            if name == variant:
                return sets
        raise ContractError(f"expected cells declare no variant {variant!r}.")

    @property
    def every_set(self) -> tuple[ExpectedCellSet, ...]:
        """The base sets and every variant's, for checks that apply to all of them."""
        return (*self.sets, *(item for _name, sets in self.variants for item in sets))

    def value_for(self, variant: str | None, column: str, measures: tuple[str, ...]) -> str | None:
        """Return the one `column` value whose rows of `variant` carry every one of `measures`.

        Serving reads a figure from the rows that carry it: pack 5's base rows carry every
        measure (`row_kind = combined`); its touch-only variant carries reach on `exposure` rows
        and outcomes on `outcome` rows. None when no one set of the variant serves them all (a
        rate whose operands sit on two branches) or the set doesn't fix `column` to one value.
        """
        for cell_set in self.for_variant(variant):
            if cell_set.serves and not set(measures) <= set(cell_set.serves):
                continue
            axis = next((item for item in cell_set.cells if item.column == column), None)
            if axis is not None and len(axis.values) == 1:
                return axis.values[0]
        return None

    def variant_pinned(self, filters: Mapping[str, str]) -> str | None:
        """Return the variant whose declared cell values `filters` pin, None for the base pack.

        A filter on a value only a variant declares (`row_kind = exposure`) reads that variant's
        series, whose publications sit under its own lens (plan D9). A filter that pins values
        of two variants can't be read from one series and is refused.
        """
        base = {
            (axis.column, value)
            for cell_set in self.sets
            for axis in (*cell_set.slices, *cell_set.cells)
            for value in axis.values
        }
        pinned = set(filters.items())
        found = {
            name
            for name, sets in self.variants
            if pinned
            & (
                {
                    (axis.column, value)
                    for cell_set in sets
                    for axis in (*cell_set.slices, *cell_set.cells)
                    for value in axis.values
                }
                - base
            )
        }
        if len(found) > 1:
            raise ValueError(f"filters pin cells of two variants: {', '.join(sorted(found))}.")
        return next(iter(found), None)


@dataclass(frozen=True, slots=True)
class MarketplaceFilterCheck:
    """Refuse a delivery whose every reached row on one slice has no outcome (design §5.5).

    Over the returned rows where `slice_column = slice_value` (packs 5 to 7: `account` IDs), if
    some row reached shoppers and every row carrying `outcome` reads 0, the marketplace filter on
    the outcome table probably didn't match the touch tables. A heuristic: a true zero week for a
    small advertiser is refused too, which turns a real zero into "not available".
    """

    slice_column: str
    slice_value: str
    reached: str
    outcome: str

    @property
    def columns(self) -> tuple[str, ...]:
        """The target columns the check reads."""
        return (self.slice_column, self.reached, self.outcome)


@dataclass(frozen=True, slots=True)
class DataContract:
    """One source dataset, at one grain, producing one curated table.

    A source document containing two grains is two contracts, never one. That is why a
    vendor sales report becomes both an aggregate and a by-ASIN contract.
    """

    contract_id: str
    contract_version: int
    source_system: str
    source_dataset: str
    source_format: SourceFormat
    target_table: str

    grain: tuple[str, ...]
    """Name the canonical columns one row is unique on. Stated, never inferred."""

    partition_keys: tuple[str, ...]
    measurement_basis: MeasurementBasis
    """Stamped as a literal during mapping, never read from a source.

    No source knows it is one of several measurement systems, which is exactly why this
    cannot be sourced and must be declared.
    """

    source_timezone: str
    """The source platform's business day. Vendor is PST always; AMC is UTC by default.

    Carried on every fact rather than normalised, because a pre-aggregated day bucket
    cannot be re-bucketed. See ADR-004.
    """

    ecosystem_key: str
    """Governed customer-facing ecosystem, never inferred from the source system."""

    platform_key: str
    """Governed business platform, independent of source system and measurement basis."""

    fields: tuple[FieldMapping, ...]
    restatement_window_days: int
    staleness_threshold_hours: int

    source_tables: tuple[str, ...] = ()
    """AMC tables this contract's SQL reads. Empty for non-AMC sources."""

    suppression: SuppressionColumns | None = None
    grain_feasibility_note: str = ""
    """Why this grain is emittable given the threshold class of every column it needs."""

    key_unique_among_unsuppressed_only: bool = False
    """Accepts that the declared grain collapses when rows merge after nulling.

    The alternative is putting the discriminator flag in the grain. One or the other is
    required, because a merged row legitimately duplicates the declared key.
    """

    currency_agreement: tuple[str, ...] = ()
    """Source currency paths that must be equal, asserted on every load.

    Required wherever several amounts collapse onto one `currency_code` (ADR-021). One
    currency column per fact keeps the model uniform across sources, but on the vendor
    side it stores less than the source gave us — each `Amount` carries its own
    `currencyCode`. This is the assertion that stops that being a silent assumption:
    on disagreement the partition quarantines rather than resolving to whichever amount
    happened to be mapped first.

    Holds source paths rather than target columns, because the values being compared
    exist only in the source document. The failure behaviour is not declared here; it is
    the same for every contract and owned by docs/capabilities/load-path/low-level-design.md §6.
    """

    tags: tuple[str, ...] = field(default=())

    expected_cells: ExpectedCells | None = None
    """The cells a shopper-aggregate delivery must return (plan D8), on every contract recording
    `cell_state` and on no other. The load writes a `not_returned` row for each that didn't."""

    currency_minimums: tuple[tuple[str, str], ...] = ()
    """Delivered currency columns paired with their delivered `MIN` twins (design §5.5).

    The pack outputs `MAX` and `MIN` of each currency over the touches or purchases behind a row;
    the two differ exactly when more than one currency reached the row, so the load refuses the
    delivery (`CURRENCY_DISAGREEMENT`) on any row where they differ. The `MIN` twin is read, never
    stored, like the three suppression columns.
    """

    marketplace_filter_check: MarketplaceFilterCheck | None = None
    """The load's marketplace-filter heuristic (design §5.5, review DS4-7), when declared."""

    repeated_totals: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Delivered totals the pack repeats on several rows, each with the delivered columns one copy
    group shares (design §5.4, §7a): a cohort's size on every follow-up month. The load refuses a
    delivery whose copies differ (`REPEATED_TOTAL_MISMATCH`)."""

    variant_follows: str | None = None
    """The contract whose published evidence decides this contract's automatic pack variant, when
    it isn't its own (plan D10): pack 15 follows pack 14's cohort grain for the same marketplace,
    so the ARP rate's two operands share one grain (Retention LLD, "One grain for both paths")."""

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ContractError("contract_id is required.")
        if self.contract_version < 1:
            raise ContractError(f"{self.contract_id}: contract_version starts at 1.")
        if not self.fields:
            raise ContractError(
                f"{self.contract_id}: a contract with no field mappings maps nothing."
            )
        if not self.grain:
            raise ContractError(
                f"{self.contract_id}: grain is required. A correct figure at an unstated "
                "grain is the failure mode this product must not have."
            )
        if not self.partition_keys:
            raise ContractError(f"{self.contract_id}: partition_keys is required.")
        if not self.target_table.startswith(TABLE_PREFIXES):
            raise ContractError(
                f"{self.contract_id}: target_table {self.target_table!r} must start with "
                f"one of {', '.join(TABLE_PREFIXES)}. The prefix is not decoration — it is "
                "what decides whether the conformed provenance columns are projected onto "
                "the table, so an unprefixed name would silently produce a fact with no "
                "basis_key."
            )
        if not self.source_timezone:
            raise ContractError(
                f"{self.contract_id}: source_timezone is required on every contract, "
                "because every fact carries it from the first load (ADR-004)."
            )
        if not self.ecosystem_key or not self.platform_key:
            raise ContractError(
                f"{self.contract_id}: ecosystem_key and platform_key are required and must be "
                "governed independently of source_system."
            )
        if self.restatement_window_days < 0:
            raise ContractError(f"{self.contract_id}: restatement_window_days cannot be negative.")
        if self.staleness_threshold_hours < 1:
            raise ContractError(
                f"{self.contract_id}: staleness_threshold_hours must be at least 1."
            )

        duplicates = sorted({c for c in self.target_columns if self.target_columns.count(c) > 1})
        if duplicates:
            raise ContractError(
                f"{self.contract_id}: two source fields map to the same target column: "
                f"{', '.join(duplicates)}."
            )
        if self.publication_gated_fields and not self.owned_grain_values:
            raise ContractError(
                f"{self.contract_id}: a served_where_published column needs a grain column the "
                "contract bounds (a literal or a value_domain), because that is what says which "
                "requests the gate applies to."
            )
        self._check_expected_cells()
        self._check_load_sanity()

    def _check_expected_cells(self) -> None:
        """Require expected cells on exactly the cell-state contracts, over their own columns."""
        if (self.expected_cells is None) == self.records_cell_state:
            raise ContractError(
                f"{self.contract_id}: a contract recording cell_state declares expected_cells, "
                "and only such a contract does (plan D8)."
            )
        if self.expected_cells is None:
            return
        written = set(self.target_columns)
        distinct = {item.target_column for item in self.fields if item.non_additive}
        for cell_set in self.expected_cells.every_set:
            unknown = sorted(set(cell_set.columns) - written)
            if unknown:
                raise ContractError(
                    f"{self.contract_id}: expected_cells name {', '.join(unknown)}, which this "
                    "contract doesn't write."
                )
            loose = sorted(set(cell_set.measures) - distinct)
            if loose:
                raise ContractError(
                    f"{self.contract_id}: expected_cells measures {', '.join(loose)} must be "
                    "this contract's non-additive measures."
                )
            unserved = sorted(set(cell_set.serves) - written)
            if unserved or (cell_set.serves and not set(cell_set.measures) <= set(cell_set.serves)):
                raise ContractError(
                    f"{self.contract_id}: expected_cells serves must name columns this contract "
                    "writes, every declared measure among them."
                )

    def _check_load_sanity(self) -> None:
        """Keep the declared load checks on columns this contract delivers or writes."""
        delivered = {item.delivered_name for item in self.fields if not item.has_no_delivered_value}
        for column, keyed_by in self.repeated_totals:
            unknown = sorted({column, *keyed_by} - delivered)
            if unknown or not keyed_by:
                raise ContractError(
                    f"{self.contract_id}: repeated_totals names {', '.join(unknown) or 'no key'}; "
                    "a repeated total and its keys are delivered columns."
                )
        if self.variant_follows == self.contract_id:
            raise ContractError(f"{self.contract_id}: variant_follows names another contract.")
        if self.marketplace_filter_check is not None:
            unknown = sorted(set(self.marketplace_filter_check.columns) - set(self.target_columns))
            if unknown:
                raise ContractError(
                    f"{self.contract_id}: marketplace_filter_check names {', '.join(unknown)}, "
                    "which this contract doesn't write."
                )
        for column, minimum in self.currency_minimums:
            if column not in delivered or minimum in delivered or not minimum.strip():
                raise ContractError(
                    f"{self.contract_id}: currency_minimums pairs a delivered currency column "
                    f"with its unmapped MIN twin; {column}/{minimum} isn't one."
                )

    @property
    def delivered_check_columns(self) -> tuple[str, ...]:
        """Delivered columns the load reads but never stores: each currency's `MIN` twin."""
        return tuple(minimum for _column, minimum in self.currency_minimums)

    @property
    def target_columns(self) -> tuple[str, ...]:
        """Every canonical column this contract writes."""
        return tuple(f.target_column for f in self.fields)

    @property
    def records_cell_state(self) -> bool:
        """Whether rows carry `cell_state`: a shopper-aggregate closed-enumeration contract.

        Such a contract is loaded under `discard_suppressed_rows` (ADR-099 point 7): no suppressed
        row reaches its fact, and an expected cell that didn't come back is written
        `cell_state = not_returned` with `not_returned` nulls. So absence is carried by the cell,
        not by a stored discriminator.
        """
        return CELL_STATE_COLUMN in self.target_columns

    @property
    def keeps_history(self) -> bool:
        """Whether this is an SCD2 dimension: its load folds rows into a validity history.

        Such a contract may load under `discard_suppressed_rows` with no `cell_state`: a withheld
        row is dropped and the history keeps the key's earlier version, so an omission never
        closes a version (packs 9 and 13, ADR-103; design §5.4).
        """
        return not self.targets_a_fact and VALID_FROM_COLUMN in self.target_columns

    @property
    def discards_into_history(self) -> bool:
        """Whether this SCD2 dimension drops its withheld rows: suppressible, no stored flag.

        A dimension that stores the discriminator (`dim_campaign`'s packs) keeps its suppressed
        rows' flag under `reject_delivery`; one that doesn't (packs 9 and 13) loads under
        `discard_suppressed_rows`, and its history preserves each omitted key's earlier version.
        """
        return (
            self.keeps_history
            and self.suppression is not None
            and self.suppression.discriminator_column not in self.target_columns
        )

    @property
    def pack_versioned_fields(self) -> tuple[FieldMapping, ...]:
        """Fields a later governed pack version added; earlier deliveries read NULL there."""
        return tuple(f for f in self.fields if f.present_from_pack_version is not None)

    @property
    def publication_gated_fields(self) -> tuple[FieldMapping, ...]:
        """Fields served only for days this contract has a CURRENT publication (plan D6)."""
        return tuple(f for f in self.fields if f.served_where_published)

    @property
    def contributes_to_coverage(self) -> bool:
        """Whether this writer's publications decide its fact's and media scope's coverage.

        False for a publication-gated writer: its days are checked per request by the planner's
        coverage gate, so it never narrows what its fact's other writers cover. Otherwise a
        reach delivery refused under `reject_delivery` would hide a day of DSP spend.
        """
        return not self.publication_gated_fields

    @property
    def monetary_fields(self) -> tuple[FieldMapping, ...]:
        """Fields carrying an amount of money."""
        return tuple(f for f in self.fields if f.monetary)

    @property
    def is_amc(self) -> bool:
        """Whether this contract reads AMC, and so is subject to threshold rules."""
        return bool(self.source_tables)

    @property
    def owned_grain_values(self) -> dict[str, tuple[str, ...]]:
        """Which values of each grain column this contract can produce, where that is knowable.

        The answer to "which rows does this load own", as far as the contract can state it. A
        grain column absent from the result is unbounded — no source enumerates campaign IDs —
        and a load must not assume anything about rows it cannot describe.

        Two ways a column becomes bounded, and they are the same claim at different widths: a
        literal is a domain of one, and a declared `value_domain` is a domain of several.
        """
        bounded: dict[str, tuple[str, ...]] = {}
        for mapping in self.fields:
            if mapping.target_column not in self.grain:
                continue
            if mapping.is_literal and mapping.literal_value is not None:
                bounded[mapping.target_column] = (mapping.literal_value,)
            elif mapping.value_domain:
                bounded[mapping.target_column] = mapping.value_domain
        return bounded

    @property
    def targets_a_fact(self) -> bool:
        """Whether this contract writes a fact rather than a dimension.

        Decided by the table-name prefix, which `__post_init__` requires. A fact is
        something measured, so it carries the basis it was measured on and the day
        boundary it was bucketed into; a dimension is something described, and carries
        neither.
        """
        return self.target_table.startswith(FACT_PREFIX)

    def field_for(self, target_column: str) -> FieldMapping | None:
        """Find the mapping producing one canonical column, if any."""
        for mapping in self.fields:
            if mapping.target_column == target_column:
                return mapping
        return None
