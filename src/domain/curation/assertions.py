# SPDX-License-Identifier: MIT-0
"""The load assertions: checks that run before any row is written.

Owned by docs/capabilities/load-path/low-level-design.md §6. Four exist; three are here. The
fourth, dimension resolution, needs `dim_product` to be loaded and lands with the SCD2 merge.

**All of them fail the same way, and the way matters more than the checks.** A failed
assertion **quarantines the partition and alarms**. It never drops a row, never coerces a
value, and never touches the last good partition — because each of these detects a condition
where the data is *plausible*, and a plausible wrong number that reaches an answer is the
failure mode this platform exists to prevent.

Pure domain, so every rule is testable against a dict without a file or a bucket. What to *do*
with a quarantine — which prefix, which alarm, which metric — is an adapter's concern.

**Header** catches missing suppression columns. Their absence means AMC may have nulled
identifying values without any discriminator, so an ordinary null cannot be distinguished from
privacy withholding and no downstream check can recover that evidence.

**Currency agreement** catches two source currencies that collapse onto one `currency_code`
disagreeing. No single value is then correct for the row, so any choice misreports an amount.

**Control total** catches detail rows that do not sum to the total the same document reports.
An incomplete array produces rows that are each correct and a total that is short — nothing is
null and nothing errors.

**Dimension resolution** catches a natural key that does not exist in its conformed dimension.
An unresolvable key lands a row whose product is unknown, and an unknown product silently drops
out of every grouped answer — the total stays plausible and the breakdown is short.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import cast

from domain.curation.mapping import SourceDocument, resolve
from domain.data_contracts.contract import DataContract
from domain.data_contracts.schema import CanonicalType, ColumnRegistry, target_schema
from domain.shared_kernel.values import DateRange

__all__ = [
    "MAX_UNRESOLVED_REPORTED",
    "AssertionName",
    "ControlTotalRequest",
    "CurrencyAgreementRequest",
    "DimensionResolutionRequest",
    "HeaderRequest",
    "LoadOutcome",
    "NonAdditiveSuppressionRequest",
    "PackVersionColumnRequest",
    "PeriodOverlapRequest",
    "Violation",
    "check_control_total",
    "check_currency_agreement",
    "check_dimension_resolution",
    "check_header",
    "check_non_additive_suppression",
    "check_pack_version_columns",
    "check_period_overlap",
    "control_total_measures",
    "detail_and_total",
    "measure_columns",
]

MAX_UNRESOLVED_REPORTED = 10
"""How many unresolved keys a violation names before summarising.

A delivery that arrives before its dimension can have thousands, and an alarm carrying all of
them is unreadable. The count is always exact; the list is a sample.
"""

GRAINS_PER_DOCUMENT = 2
"""A vendor document carries an aggregate array and a by-ASIN array, so exactly two contracts
read it. A dataset with any other number is not a detail-and-total pair."""

ONE_CURRENCY = 1
"""A rollup describes one marketplace, so it has exactly one currency. More is the violation."""

NUMERIC = frozenset(
    {
        CanonicalType.INT,
        CanonicalType.BIGINT,
        CanonicalType.DOUBLE,
        CanonicalType.DECIMAL,
    }
)
"""Types a control total can be summed over. A `string` currency code and a `timestamp`
watermark are on both contracts and are emphatically not measures."""


class AssertionName(StrEnum):
    """Which assertion failed. Dimensioned on in the error metric, so it is a closed set."""

    HEADER = "header"
    NON_ADDITIVE_SUPPRESSION = "non_additive_suppression"
    CURRENCY_AGREEMENT = "currency_agreement"
    CONTROL_TOTAL = "control_total"
    DIMENSION_RESOLUTION = "dimension_resolution"

    PERIOD_OVERLAP = "period_overlap"
    """This delivery's period intersects one already loaded for the same contract and option set.

    **The only assertion here whose failure produces a plausible number rather than a missing
    one.** A fact partition is identified by its period bounds, so two deliveries whose windows
    intersect without matching are two distinct live partitions — and a question spanning them
    reads both, counting every shared day twice. Nothing is null, nothing errors, and the total
    is wrong by exactly the overlap.

    It exists because a *scheduled* pull makes this likely rather than exotic. "A rolling
    fourteen days, daily" has two readings: re-pull fourteen fixed one-day periods, each
    superseding its own predecessor, or slide a fourteen-day window and land thirteen duplicated
    days every time. Only the first is correct and the second is the obvious implementation.

    Equality is deliberately not a violation. An identical re-pull is what `APPLY_RESTATEMENT`
    and `SKIP_SUPERSEDED` are for: it replaces its partition wholesale rather than landing beside
    it, which is the mechanism a maturing attribution window depends on.
    """

    PRIVACY_BAR = "privacy_bar"
    """A shopper-aggregate delivery's unsuppressed rows read `distinct_user_count` above the
    pack's `expectedBar` (ADR-099 point 10).

    Under a bar of 2 the column reads at most 2, so a larger value means a MEDIUM or HIGH column
    reached the output and ADR-034's binary suppression property no longer holds. Every row at
    exactly 100 under a bar of 2 is recorded as an escalation, still a rejection.
    """
    PACK_VARIANT = "pack_variant"
    """A delivery is a different declared pack variant from the one this period expects
    (`PACK_VARIANT_MISMATCH`, plan D10): fine and coarse rows must never mix in one series."""
    REPEATED_TOTAL = "repeated_total"
    """A total a shopper-aggregate pack repeats on several rows differs between two copies
    (`REPEATED_TOTAL_MISMATCH`, design §7a): a cohort's size must read the same on every
    follow-up month row of one delivery."""
    MARKETPLACE_FILTER = "marketplace_filter"
    """Every reached `account` row of a shopper-aggregate delivery has no purchaser, the sign of a
    marketplace filter that missed the outcome table (`MARKETPLACE_FILTER_SUSPECTED`, design §5.5
    review DS4-7). A heuristic: a real zero week is refused too, the conservative error."""
    CLOSED_ENUMERATION = "closed_enumeration"
    """A shopper-aggregate delivery breaks its declared cells: an undeclared or NULL cell value,
    a repeated cell, or a closed-enumeration contract loaded without `discard_suppressed_rows`
    (ADR-099 point 8)."""
    PACK_VERSION_COLUMN = "pack_version_column"
    """A delivery produced at a pack version that outputs a column has a row with it NULL.

    The column is nullable only so an earlier pack version's deliveries load
    (`FieldMapping.present_from_pack_version`). The pack's `SUM` is taken to return a value on
    every kept row [unverified: no live AMC run of the version yet], so a NULL from a version that
    outputs it is treated as a defect: loading it would read as an earlier version's "not
    carried" (the pack-version null meaning in docs/canonical-model.md). It blocks the whole
    delivery (docs/capabilities/load-path/low-level-design.md, "Pack-versioned columns").
    """
    MANIFEST_AGREEMENT = "manifest_agreement"
    """The manifest and the document disagree about which pull this delivery is.

    Added with the landing contract (ADR-030), which makes the manifest authoritative about a
    delivery's identity. That claim needs one check against the bytes or it is only a
    convention: a manifest saying `MANUFACTURING` beside a `SOURCING` document would partition
    the rows under the wrong option hash, and two lenses on the same units would merge and
    double-count every one of them.

    Widening a closed metric dimension is a real change, and it earns it here because this is
    the only assertion that can catch a provenance lie rather than a data defect.
    """


@dataclass(frozen=True, slots=True)
class Violation:
    """One reason a partition must quarantine.

    `detail` is written for a human reading an alarm. `context` carries the structured values
    for the log line, kept separate because a description can reach a caller while context is
    only ever logged.
    """

    assertion: AssertionName
    detail: str
    context: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.detail:
            raise ValueError(f"a {self.assertion} violation must say what failed.")


@dataclass(frozen=True, slots=True)
class LoadOutcome:
    """Every violation found, never only the first.

    Reporting all of them together is the same rule `getConfig` and the contract gate follow:
    discovering the next problem on the next load turns one correction into a week of them.
    """

    violations: tuple[Violation, ...] = ()

    @property
    def quarantines(self) -> bool:
        """Whether the partition must be quarantined rather than written."""
        return bool(self.violations)

    def combined(self, other: LoadOutcome) -> LoadOutcome:
        """Merge two outcomes, so a caller can run every assertion before deciding."""
        return LoadOutcome(violations=(*self.violations, *other.violations))


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeaderRequest:
    """The delivered object's own column names, against what the contract requires."""

    contract: DataContract
    delivered_columns: frozenset[str]


def check_header(request: HeaderRequest) -> LoadOutcome:
    """Verify the declared suppression columns are present in the delivered object.

    **The one assertion whose failure is unrecoverable.** Without the discriminator AMC may
    null an identifying value without saying that privacy filtering caused it. The delivered
    null is then indistinguishable from an ordinary absent value, so no later check can recover
    complete suppression evidence.

    Scoped to contracts that declare `suppression`, which is every AMC contract whose totals
    must reconcile. A vendor report has no threshold mechanism, so there is nothing to check.
    """
    contract = request.contract
    if contract.suppression is None:
        return LoadOutcome()

    # Read from the suppression block rather than from the field mappings, because what must have
    # *arrived* is what the extraction was asked for — and that is no longer the same set as what
    # the table stores. A fact several contracts write does not conform the two diagnostic columns
    # (ADR-033, G-28), so a mapping-derived list would stop checking they were requested at all,
    # and a missing diagnostic is still evidence the extraction options were wrong.
    #
    # The delivered names, not the source names. An extraction-option column is aliased to its
    # canonical name by the extraction (docs/capabilities/data-layer/low-level-design.md §6,
    # Delivered column names).
    required = set(contract.suppression.all_columns)
    missing = sorted(required - request.delivered_columns)
    if not missing:
        return LoadOutcome()

    return LoadOutcome(
        violations=(
            Violation(
                assertion=AssertionName.HEADER,
                detail=(
                    f"{contract.contract_id}: the delivered object is missing "
                    f"{', '.join(missing)}. The extraction did not set the suppression "
                    "options, so privacy-null values are indistinguishable from ordinary "
                    "nulls and the delivery has no complete suppression evidence."
                ),
                context=(
                    ("contract_id", contract.contract_id),
                    ("missing_columns", ",".join(missing)),
                ),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Pack-version columns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackVersionColumnRequest:
    """One mapped rowset and the query pack version its manifest's governance names."""

    contract: DataContract
    query_pack_version: int | None
    """`None` for an ungoverned delivery, which no pack produced, so no column is versioned."""
    rows: tuple[dict[str, object], ...]


def check_pack_version_columns(request: PackVersionColumnRequest) -> LoadOutcome:
    """Refuse a delivery whose pack outputs a versioned column but left it NULL on a row.

    A delivery from an earlier pack version doesn't carry the column, so its rows read NULL and
    pass: the column is nullable with `not_applicable_under_options` for exactly that case. One
    from the version that outputs it must carry a value on every row.
    """
    version = request.query_pack_version
    if version is None:
        return LoadOutcome()
    violations: list[Violation] = []
    for field in request.contract.pack_versioned_fields:
        if version < cast("int", field.present_from_pack_version):
            continue
        empty = sum(1 for row in request.rows if row.get(field.target_column) is None)
        if not empty:
            continue
        violations.append(
            Violation(
                assertion=AssertionName.PACK_VERSION_COLUMN,
                detail=(
                    f"{request.contract.contract_id}: {empty} row(s) carry no "
                    f"{field.target_column}, but query pack version {version} outputs it "
                    f"(present from version {field.present_from_pack_version}). Only a delivery "
                    "from an earlier pack version may leave it NULL."
                ),
                context=(
                    ("contract_id", request.contract.contract_id),
                    ("column", field.target_column),
                    ("query_pack_version", str(version)),
                    ("null_rows", str(empty)),
                ),
            )
        )
    return LoadOutcome(violations=tuple(violations))


# ---------------------------------------------------------------------------
# Non-additive suppression
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NonAdditiveSuppressionRequest:
    """One mapped contract rowset before withheld rows are folded or published."""

    contract: DataContract
    rows: tuple[dict[str, object], ...]


def check_non_additive_suppression(
    request: NonAdditiveSuppressionRequest,
) -> LoadOutcome:
    """Refuse a delivery that suppresses any row carrying a non-additive measure.

    Live AMC evidence on 2026-08-27 showed discriminator mode preserving duplicate fully-nulled
    grain rows when `COUNT(DISTINCT user_id)` is selected. Folding or aggregating those rows would
    sum distinct counts; keeping them violates the declared key. A clean delivery is usable, but
    one suppressed row makes the whole non-additive result unsafe.
    """
    measures = sorted(
        field.target_column for field in request.contract.fields if field.non_additive
    )
    if not measures:
        return LoadOutcome()
    states: list[bool] = []
    invalid = 0
    for row in request.rows:
        value = row.get("rows_suppressed")
        if isinstance(value, bool):
            states.append(value)
        elif isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            states.append(value.strip().lower() == "true")
        else:
            invalid += 1
    if invalid:
        return LoadOutcome(
            violations=(
                Violation(
                    assertion=AssertionName.NON_ADDITIVE_SUPPRESSION,
                    detail=(
                        f"{request.contract.contract_id}: {invalid} row(s) carry an invalid "
                        "rows_suppressed value, so non-additive suppression safety is unknown."
                    ),
                    context=(
                        ("contract_id", request.contract.contract_id),
                        ("invalid_suppression_rows", str(invalid)),
                    ),
                ),
            )
        )
    suppressed = sum(states)
    if suppressed == 0:
        return LoadOutcome()
    return LoadOutcome(
        violations=(
            Violation(
                assertion=AssertionName.NON_ADDITIVE_SUPPRESSION,
                detail=(
                    f"{request.contract.contract_id}: {suppressed} suppressed row(s) carry "
                    f"non-additive {', '.join(measures)}. AMC can preserve duplicate nulled "
                    "grain rows for distinct counts, so this delivery cannot be aggregated or "
                    "published safely."
                ),
                context=(
                    ("contract_id", request.contract.contract_id),
                    ("measures", ",".join(measures)),
                    ("suppressed_rows", str(suppressed)),
                ),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Currency agreement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrencyAgreementRequest:
    """A parsed document and the rows mapped from it.

    Both are needed because the two halves of the check look in different places: the declared
    agreement paths exist only in the source document, and the collapsed `currency_code` exists
    only on the mapped row.
    """

    contract: DataContract
    source: SourceDocument
    rows: tuple[dict[str, object], ...]


def _row_currencies(contract: DataContract, source: SourceDocument) -> list[dict[str, str]]:
    """Resolve every declared agreement path, per array element."""
    if source.document is None:
        return []
    arrays = {
        path.split("[]", maxsplit=1)[0] for path in contract.currency_agreement if "[]" in path
    }
    if len(arrays) != 1:
        return []
    elements = resolve(source.document, next(iter(arrays)))
    if not isinstance(elements, list):
        return []
    element_count = len(cast("list[object]", elements))

    found: list[dict[str, str]] = []
    for index in range(element_count):
        per_row: dict[str, str] = {}
        for path in contract.currency_agreement:
            array_name, _, remainder = path.partition("[].")
            value = resolve(resolve(source.document, f"{array_name}[{index}]"), remainder)
            if isinstance(value, str) and value:
                per_row[path] = value
        found.append(per_row)
    return found


def check_currency_agreement(request: CurrencyAgreementRequest) -> LoadOutcome:
    """Verify every currency collapsing onto one column agrees, per ADR-021.

    Two checks, because there are two ways a document can carry more than one currency and
    only one of them is what ADR-021 names.

    **Within a row.** A vendor sales report gives three `Amount` objects each with its own
    `currencyCode`, and the model keeps one `currency_code` per fact. ADR-021 accepts that
    narrowing *on condition that agreement is asserted* — otherwise the shared column is a
    silent assumption, and revenue in USD beside COGS in EUR would be stored as though both
    were the mapped currency.

    **Across the document.** One report is one marketplace — `marketplaceIds` is on
    `reportSpecification` and never on a row — and ADR-021 reasons from `dim_marketplace`
    carrying one default currency that one row is one marketplace. So two rows of one report
    disagreeing means the document contradicts itself, and no single currency describes the
    rollup.

    Absent currencies are skipped rather than treated as disagreement: a `SOURCING` pull omits
    `orderedRevenue` entirely, so its `currencyCode` is legitimately missing.
    """
    contract = request.contract
    violations: list[Violation] = []

    for index, per_row in enumerate(_row_currencies(contract, request.source)):
        distinct = sorted(set(per_row.values()))
        if len(distinct) > ONE_CURRENCY:
            violations.append(
                Violation(
                    assertion=AssertionName.CURRENCY_AGREEMENT,
                    detail=(
                        f"{contract.contract_id}: row {index} reports "
                        f"{' and '.join(distinct)} across amounts that collapse onto one "
                        "currency_code, so no single value is correct for the row."
                    ),
                    context=(
                        ("contract_id", contract.contract_id),
                        ("row_index", str(index)),
                        ("currencies", ",".join(distinct)),
                        (
                            "paths",
                            ",".join(f"{path}={value}" for path, value in sorted(per_row.items())),
                        ),
                    ),
                )
            )

    across = sorted(
        {str(row["currency_code"]) for row in request.rows if row.get("currency_code") is not None}
    )
    if len(across) > ONE_CURRENCY:
        violations.append(
            Violation(
                assertion=AssertionName.CURRENCY_AGREEMENT,
                detail=(
                    f"{contract.contract_id}: the document reports {' and '.join(across)} "
                    "across its rows. One report covers one marketplace, so a single "
                    "currency should describe every row and this rollup has none."
                ),
                context=(
                    ("contract_id", contract.contract_id),
                    ("currencies", ",".join(across)),
                ),
            )
        )

    return LoadOutcome(violations=tuple(violations))


# ---------------------------------------------------------------------------
# Control total
# ---------------------------------------------------------------------------


def measure_columns(contract: DataContract, registry: ColumnRegistry) -> frozenset[str]:
    """Name the columns a control total can be summed over.

    Derived rather than listed: a measure is a numeric column that is neither part of the grain
    nor a partition key. That excludes `date_key` and `marketplace_key`, which are numeric-free
    anyway, and correctly excludes `currency_code` and `source_watermark`, which appear on both
    contracts and are not quantities.
    """
    structural = set(contract.grain) | set(contract.partition_keys)
    return frozenset(
        column.name
        for column in target_schema(contract, registry)
        if column.type in NUMERIC and column.name not in structural
    )


def control_total_measures(contract: DataContract, registry: ColumnRegistry) -> frozenset[str]:
    """Name the measures a control total checks: every measure except a distinct count.

    A distinct count isn't additive, so neither its detail rows nor a fact plus its residual add
    up to a reported total (ADR-099 point 12). It is skipped rather than checked and failed.
    """
    distinct = {field.target_column for field in contract.fields if field.non_additive}
    return frozenset(measure_columns(contract, registry) - distinct)


def detail_and_total(
    contracts: list[DataContract],
) -> list[tuple[DataContract, DataContract]]:
    """Pair the contracts that read one document at two grains.

    Derived from the contracts rather than configured: two contracts sharing a `source_dataset`
    while writing different tables are the two grains of one document, and the total is the one
    whose grain is a strict subset — the by-ASIN contract's extra key *is* the difference
    between them.
    """
    by_dataset: dict[str, list[DataContract]] = {}
    for contract in contracts:
        by_dataset.setdefault(contract.source_dataset, []).append(contract)

    pairs: list[tuple[DataContract, DataContract]] = []
    for group in by_dataset.values():
        if len(group) != GRAINS_PER_DOCUMENT:
            continue
        first, second = group
        if set(first.grain) < set(second.grain):
            pairs.append((second, first))
        elif set(second.grain) < set(first.grain):
            pairs.append((first, second))
    return pairs


@dataclass(frozen=True, slots=True)
class ControlTotalRequest:
    """One document read through both of its contracts."""

    detail_rows: tuple[dict[str, object], ...]
    total_rows: tuple[dict[str, object], ...]
    measures: frozenset[str]
    label: str = "control total"


def _sum_of(rows: tuple[dict[str, object], ...], column: str) -> Decimal | None:
    """Total one column across the detail rows, or None when no row reports it.

    None rather than zero, because "every row omitted this measure" and "every row reported
    zero" are different facts — a `SOURCING` pull does the first and would look like the
    second.
    """
    present = [row[column] for row in rows if row.get(column) is not None]
    if not present:
        return None
    return sum((Decimal(str(value)) for value in present), start=Decimal(0))


def check_control_total(request: ControlTotalRequest) -> LoadOutcome:
    """Verify the detail rows sum to the total the same document reports.

    The failure this catches leaves nothing null and raises nothing: a truncated array produces
    rows that are individually correct and a total that is quietly short, so every figure looks
    reasonable and the sum is wrong.

    **Exact equality, deliberately, and this may need revisiting against a real file.** A
    tolerance chosen now would be a guess, and the condition being guarded against — an array
    missing whole ASINs — is off by far more than rounding. Quarantining on a rounding
    difference is the safe direction to be wrong in; accepting a short total is not.
    """
    if not request.total_rows:
        return LoadOutcome(
            violations=(
                Violation(
                    assertion=AssertionName.CONTROL_TOTAL,
                    detail=(
                        f"{request.label}: the document reports no aggregate row, so the "
                        "detail rows cannot be checked against anything."
                    ),
                    context=(("detail_rows", str(len(request.detail_rows))),),
                ),
            )
        )

    violations: list[Violation] = []
    for column in sorted(request.measures):
        detail = _sum_of(request.detail_rows, column)
        reported = _sum_of(request.total_rows, column)
        if detail == reported:
            continue

        # One side absent and the other present is a disagreement rather than a rounding
        # question: the report cannot both omit a measure and report it.
        violations.append(
            Violation(
                assertion=AssertionName.CONTROL_TOTAL,
                detail=(
                    f"{request.label}: {column} sums to "
                    f"{'nothing' if detail is None else detail} across "
                    f"{len(request.detail_rows)} detail rows, but the document reports "
                    f"{'nothing' if reported is None else reported}."
                ),
                context=(
                    ("column", column),
                    ("detail_sum", "null" if detail is None else str(detail)),
                    ("reported", "null" if reported is None else str(reported)),
                    ("detail_rows", str(len(request.detail_rows))),
                ),
            )
        )
    return LoadOutcome(violations=tuple(violations))


# ---------------------------------------------------------------------------
# Period overlap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodOverlapRequest:
    """This delivery's period against the periods already loaded for the same partition family."""

    contract: DataContract
    period: DateRange
    loaded: tuple[DateRange, ...] = ()
    """Periods already loaded for this contract under **this delivery's** option set and
    marketplace, and nothing wider.

    Pre-filtered by the caller, the same shape `known_keys` takes: the store is asked a narrow
    question and the assertion judges the answer. Filtering matters and is not incidental — two
    `report_options_hash` values are the same units under two lenses (`PartitionKey`), so periods
    from another option set overlap legitimately and comparing against them would quarantine
    every correct delivery.

    Only *completed* loads belong here. A quarantined partition wrote nothing, so sharing days
    with it duplicates nothing.
    """


def check_period_overlap(request: PeriodOverlapRequest) -> LoadOutcome:
    """Verify this delivery's period does not intersect one already loaded.

    **Scoped to facts by the caller**, because the failure is arithmetic rather than structural.
    A fact is summed across partitions, so a shared day is counted once per partition holding it.
    A dimension is merged by natural key into SCD2 versions, so two intersecting windows produce
    version history rather than a doubled measure — which is why `dim_product` legitimately has
    intersecting deliveries landed today.

    An exactly equal period is not a violation. That is a restatement, and the watermark decides
    it: the partition is replaced rather than added to.
    """
    clashing = tuple(
        loaded
        for loaded in request.loaded
        if loaded != request.period and loaded.overlaps(request.period)
    )
    if not clashing:
        return LoadOutcome()

    readable = ", ".join(str(period) for period in sorted(clashing, key=lambda p: (p.start, p.end)))
    return LoadOutcome(
        violations=(
            Violation(
                assertion=AssertionName.PERIOD_OVERLAP,
                detail=(
                    f"{request.contract.contract_id}: this delivery covers {request.period} "
                    f"which intersects {len(clashing)} period(s) already loaded under the same "
                    f"option set — {readable}. A fact is identified by its period bounds, so "
                    "both partitions would stay live and every shared day would be counted "
                    "twice. Re-pull a fixed period so the load supersedes, rather than sliding "
                    "the window."
                ),
                context=(
                    ("contract_id", request.contract.contract_id),
                    ("period", str(request.period)),
                    ("overlapping_periods", readable),
                ),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Dimension resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DimensionResolutionRequest:
    """Fact rows against the keys a conformed dimension currently holds."""

    contract: DataContract
    rows: tuple[dict[str, object], ...]
    known_keys: frozenset[tuple[object, ...]]
    """Every key in force in the dimension, as a tuple per `join_columns`."""

    join_columns: tuple[str, ...]
    """The columns the fact joins the dimension on, in order. For `dim_product` that is
    `(amazon_asin, marketplace_key)` — the natural key, since there is no surrogate
    (ADR-023)."""

    dimension: str = "dim_product"

    def __post_init__(self) -> None:
        if not self.join_columns:
            raise ValueError("a resolution check needs the columns to join on.")


def check_dimension_resolution(request: DimensionResolutionRequest) -> LoadOutcome:
    """Verify every natural key on a fact resolves in its conformed dimension.

    **The failure is a shortfall, not an error.** A row whose product is unknown still writes,
    still carries correct measures, and then vanishes from every answer grouped by product —
    so the grand total looks right while every breakdown is short by that row. Quarantining
    instead means the partition is absent and visibly so.

    Rows whose join columns are entirely null are skipped: a fact legitimately without a product
    grain has nothing to resolve, and reporting it would make the assertion fire on tables that
    correctly carry no ASIN at all.
    """
    unresolved: dict[tuple[object, ...], int] = {}
    for row in request.rows:
        key = tuple(row.get(column) for column in request.join_columns)
        if all(value is None for value in key):
            continue
        if key not in request.known_keys:
            unresolved[key] = unresolved.get(key, 0) + 1

    if not unresolved:
        return LoadOutcome()

    sample = sorted((tuple(str(value) for value in key) for key in unresolved), key=lambda k: k)[
        :MAX_UNRESOLVED_REPORTED
    ]
    readable = "; ".join("/".join(key) for key in sample)
    more = len(unresolved) - len(sample)

    return LoadOutcome(
        violations=(
            Violation(
                assertion=AssertionName.DIMENSION_RESOLUTION,
                detail=(
                    f"{request.contract.contract_id}: {len(unresolved)} key(s) across "
                    f"{sum(unresolved.values())} row(s) do not resolve in {request.dimension} "
                    f"on {'/'.join(request.join_columns)}. These rows would write correctly and "
                    f"then drop out of every grouped answer. Unresolved: {readable}"
                    + (f" and {more} more" if more > 0 else "")
                ),
                context=(
                    ("contract_id", request.contract.contract_id),
                    ("dimension", request.dimension),
                    ("join_columns", ",".join(request.join_columns)),
                    ("unresolved_keys", str(len(unresolved))),
                    ("affected_rows", str(sum(unresolved.values()))),
                ),
            ),
        )
    )
