# SPDX-License-Identifier: MIT-0
"""Merge the contracts writing one curated table into a single table definition.

A table is not one contract's output. `fct_ad_exposure` is written by four, one per AMC
dataset, because no single AMC table carries impressions, clicks and viewability together
(ADR-022). Each contributes different measures at the same grain, so the table's column
list is the union of theirs.

Two merge rules, and both exist to prevent a specific wrong answer:

**A type conflict is fatal.** If two contracts claim different types for one column, one of
them is wrong and there is no safe way to choose. Emitting either would make the load that
disagrees fail at write time — or worse, coerce.

**A column absent from any contributing contract is nullable.** `amc-dsp-clicks` produces
`clicks` and not `impressions`, so its rows store NULL there. Requiring it would reject a
legitimate partial write.

**What that absence means is the union's fill, and it differs by additivity (ADR-109).** An
additive column is zero-filled: a writer that doesn't supply `impressions` contributes none to
the sum, which is what `SUM` over its NULL computes (ADR-022). A column every writer declares
`non_additive` is null-filled: a campaign-day with no reach delivery reached nobody we know of,
not nobody at all, so it is never read as 0. Both are stored as NULL (ADR-037); the fill is the
declaration of how a reader may treat that NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domain.data_contracts.contract import DataContract
from domain.data_contracts.schema import (
    CONFORMED_PROVENANCE_COLUMNS,
    CanonicalType,
    ColumnRegistry,
    TargetColumn,
    target_schema,
)


class MergeError(Exception):
    """Two contracts disagree about a column they both write."""


class AbsentFill(StrEnum):
    """How a column only some writers supply is filled where a writer lacks it (ADR-109)."""

    ZERO = "zero"
    """Additive: the missing writer contributes nothing to a sum."""
    NULL = "null"
    """Every writer declares it `non_additive`: the missing writer's rows have no value, never 0."""


@dataclass(frozen=True, slots=True)
class MergedColumn:
    """One column of the generated table, after merging every contract that writes it."""

    name: str
    type: CanonicalType
    nullable: bool
    written_by: tuple[str, ...]
    provenance: tuple[str, ...]
    null_meaning: str | None
    absent_fill: AbsentFill | None = None
    """None when every contract writing the table supplies the column, so nothing is filled."""


@dataclass(frozen=True, slots=True)
class MergedTable:
    """One curated table, generated from every contract that writes it."""

    name: str
    columns: tuple[MergedColumn, ...]
    partition_keys: tuple[str, ...]
    grain: tuple[str, ...]
    measurement_bases: tuple[str, ...]
    written_by: tuple[str, ...]

    @property
    def is_dimension(self) -> bool:
        """Dimensions load by SCD2 merge rather than partition overwrite."""
        return self.name.startswith("dim_")


def merge_tables(
    contracts: list[DataContract], registry: ColumnRegistry
) -> tuple[MergedTable, ...]:
    """Group contracts by target table and merge each group into one definition."""
    by_table: dict[str, list[DataContract]] = {}
    for contract in contracts:
        by_table.setdefault(contract.target_table, []).append(contract)

    return tuple(_merge_one(name, group, registry) for name, group in sorted(by_table.items()))


def _merge_one(name: str, group: list[DataContract], registry: ColumnRegistry) -> MergedTable:
    schemas: dict[str, tuple[TargetColumn, ...]] = {
        c.contract_id: target_schema(c, registry) for c in group
    }
    contract_ids = tuple(sorted(schemas))

    seen: dict[str, MergedColumn] = {}
    for contract_id in contract_ids:
        for column in schemas[contract_id]:
            existing = seen.get(column.name)
            if existing is None:
                seen[column.name] = MergedColumn(
                    name=column.name,
                    type=column.type,
                    nullable=column.nullable,
                    written_by=(contract_id,),
                    provenance=(f"{contract_id}: {column.provenance}",),
                    null_meaning=column.null_meaning,
                )
                continue
            if existing.type is not column.type:
                raise MergeError(
                    f"{name}.{column.name}: {existing.written_by[0]} types it "
                    f"{existing.type.value} and {contract_id} types it "
                    f"{column.type.value}. One is wrong, and there is no safe default."
                )
            seen[column.name] = MergedColumn(
                name=existing.name,
                type=existing.type,
                nullable=existing.nullable or column.nullable,
                written_by=(*existing.written_by, contract_id),
                provenance=(*existing.provenance, f"{contract_id}: {column.provenance}"),
                null_meaning=existing.null_meaning or column.null_meaning,
            )

    # A column only some contracts write must tolerate absence, and declares how it is filled.
    by_id = {contract.contract_id: contract for contract in group}
    columns = tuple(
        MergedColumn(
            name=c.name,
            type=c.type,
            nullable=c.nullable or len(c.written_by) < len(contract_ids),
            written_by=c.written_by,
            provenance=c.provenance,
            null_meaning=c.null_meaning,
            absent_fill=(
                None
                if len(c.written_by) == len(contract_ids)
                else _absent_fill(c.name, tuple(by_id[writer] for writer in c.written_by))
            ),
        )
        # Keys first, then everything else alphabetically, so a generated schema is
        # readable and its diff is stable.
        for c in sorted(seen.values(), key=lambda x: (not x.name.endswith("_key"), x.name))
    )

    partitions = {k for c in group for k in c.partition_keys}
    grains = {g for c in group for g in c.grain}
    bases = {c.measurement_basis.value for c in group}

    return MergedTable(
        name=name,
        columns=columns,
        partition_keys=tuple(sorted(partitions)),
        grain=tuple(sorted(grains)),
        measurement_bases=tuple(sorted(bases)),
        written_by=contract_ids,
    )


def _absent_fill(column: str, writers: tuple[DataContract, ...]) -> AbsentFill:
    """NULL when every writer of the column declares it `non_additive`; ZERO otherwise (ADR-109)."""
    mappings = tuple(writer.field_for(column) for writer in writers)
    if mappings and all(mapping is not None and mapping.non_additive for mapping in mappings):
        return AbsentFill.NULL
    return AbsentFill.ZERO


def curated_columns(table: MergedTable) -> tuple[TargetColumn, ...]:
    """Project a merged table back onto the column type the composers take.

    The write path needs the *table's* nullability rather than one contract's, for the same
    reason `dq.py` does: a column only some contracts write is nullable, and `target_schema`
    cannot know that because it sees one contract. `column_scoped_merge` decides whether it may
    withdraw a column, and answering from the contract would say no for every measure on
    `fct_ad_exposure` — each contract declares its own measures non-nullable, while the table
    that was actually deployed permits null in all of them.

    Provenance is joined rather than dropped so the value stays self-describing; nothing on the
    write path reads it, but a column that cannot say where it came from is how a debugging
    session starts from scratch.
    """
    return tuple(
        TargetColumn(
            name=column.name,
            type=column.type,
            nullable=column.nullable,
            provenance=" · ".join(column.provenance),
            null_meaning=column.null_meaning,
        )
        for column in table.columns
    )


WITHHELD_SUFFIX = "_withheld"
"""What a residual table is called: its fact's name plus this.

Derived from the fact's name rather than declared, so a new suppressible fact cannot acquire a
residual table with a name nobody expects — and so the pairing is readable in a bucket listing.
"""

CONTRACT_ID = "contract_id"
PERIOD_START = "period_start"
PERIOD_END = "period_end"
OPTION_SET_HASH = "option_set_hash"

WITHHELD_GRAIN: tuple[str, ...] = (
    CONTRACT_ID,
    "marketplace_key",
    PERIOD_START,
    PERIOD_END,
    OPTION_SET_HASH,
)
"""What identifies one withheld residual, and every column comes from the manifest.

**That is the entire decision.** AMC nulls the row's own dimensions, so a residual keyed on them
collides across windows — measured, see
[ADR-034](../../../../docs/decisions/adr-034-a-withheld-residual-is-keyed-on-its-delivery.md).
Every column here is authored by us when the delivery is landed, so suppression cannot reach it.

`contract_id` is in the grain because four contracts write `fct_ad_exposure` and each withholds its
own measures. That makes their residual rows provably disjoint, which is ADR-033's separability
question answered by construction rather than by a predicate.
"""

WITHHELD_PARTITION_KEYS: tuple[str, ...] = ("marketplace_key", PERIOD_START)
"""Mirrors the fact's own `(marketplace_key, date_key)` shape, with a window start for a date.

A residual spans its delivery's window rather than sitting on a day, and partitioning on the start
keeps a listing readable without implying the figures belong to that one date — the grain carries
both bounds, so nothing can group a residual by a single day.
"""


RESIDUAL_ROW_KIND = "row_kind"
"""A fact's variant row kind (plan D11), a fact grain column a residual also keys on.

Pack 5's touch-only variant withholds exposure and outcome rows in one delivery; without the row
kind their residual figures would collide under the delivery's key. It is a literal the pack's
SELECT stamps, which suppression leaves on the row, so the key stays unreachable by suppression.
"""

RESIDUAL_BUCKET_SCHEME = "bucket_scheme"
"""A frequency fact's bucket scheme (`fine` or `coarse`, plan D11), a fact grain column a residual
also keys on.

Packs 6 and 7 stamp it as a literal on every row, a withheld one included, so a residual names the
scheme its withheld buckets were counted under from the table's creation rather than leaving a
reader to recover it from the option set behind `option_set_hash`.
"""

RESIDUAL_FACT_KEYS: tuple[tuple[str, CanonicalType, str], ...] = (
    (
        RESIDUAL_ROW_KIND,
        CanonicalType.STRING,
        "the withheld rows' row_kind, stamped by the pack's SELECT",
    ),
    (
        RESIDUAL_BUCKET_SCHEME,
        CanonicalType.STRING,
        "the withheld rows' bucket_scheme, stamped by the pack's SELECT",
    ),
    ("as_of_month", CanonicalType.DATE, "the delivery's manifest: the vintage's last month"),
    (
        "cohort_grain",
        CanonicalType.STRING,
        "the withheld rows' cohort_grain, stamped by the pack's SELECT",
    ),
    ("cohort_path", CanonicalType.STRING, "the contract's cohort_path literal"),
)
"""The fact grain keys a residual keys on where its fact has them, in column order: pack 5's row
kind, packs 6-7's bucket scheme, and packs 14-15's vintage, cohort grain and path (plan D11)."""


def withheld_table(fact: MergedTable) -> MergedTable:
    """Derive the residual table that carries what one delivery could not attribute.

    Only for a fact whose contracts declare suppression — `withheld_tables` decides that. The
    columns are the fact's own measures, so the two tables stay comparable figure for figure, plus
    a grain taken from the manifest and the conformed provenance every fact carries, and the
    fact's `row_kind` and `bucket_scheme` where it has them (`RESIDUAL_FACT_KEYS`), from the
    table's creation.

    Every measure is nullable here even where the fact requires it. A residual reports what a
    delivery withheld, and a contract that withheld nothing for a column has nothing to put in it.
    """
    measures = tuple(
        column
        for column in fact.columns
        if column.name not in fact.grain and column.name not in CONFORMED_PROVENANCE_COLUMNS
    )
    keys = tuple(
        MergedColumn(
            name=name,
            type=CanonicalType.DATE if name in {PERIOD_START, PERIOD_END} else CanonicalType.STRING,
            nullable=False,
            written_by=fact.written_by,
            provenance=(f"the delivery's manifest: {name}",),
            null_meaning=None,
        )
        for name in WITHHELD_GRAIN
    )
    for name, key_type, provenance_text in RESIDUAL_FACT_KEYS:
        literal = next((column for column in fact.columns if column.name == name), None)
        if literal is None:
            continue
        keys = (
            *keys,
            MergedColumn(
                name=name,
                type=key_type,
                nullable=False,
                written_by=literal.written_by,
                provenance=(provenance_text,),
                null_meaning=None,
            ),
        )
    grain = tuple(column.name for column in keys)
    provenance = tuple(
        column for column in fact.columns if column.name in CONFORMED_PROVENANCE_COLUMNS
    )
    withheld = tuple(
        MergedColumn(
            name=column.name,
            type=column.type,
            nullable=True,
            written_by=column.written_by,
            provenance=column.provenance,
            null_meaning=column.null_meaning,
        )
        for column in measures
    )
    return MergedTable(
        name=f"{fact.name}{WITHHELD_SUFFIX}",
        columns=(*keys, *provenance, *withheld),
        partition_keys=WITHHELD_PARTITION_KEYS,
        grain=grain,
        measurement_bases=fact.measurement_bases,
        written_by=fact.written_by,
    )


def withheld_tables(
    tables: tuple[MergedTable, ...], contracts: list[DataContract]
) -> tuple[MergedTable, ...]:
    """Derive a residual table for every fact that can withhold, and for no other.

    Derived from whether the contracts writing a fact declare a `suppression` block, never from a
    per-table setting. A vendor fact has no threshold mechanism, so a residual table for one would
    be a table nothing could ever write.

    A suppressible contract counts only when it maps an additive measure. The residual excludes
    distinct counts (ADR-099 point 12), so a fact whose suppressible writers deliver nothing but
    distinct counts — a shopper aggregate — has nothing a residual could hold.
    """
    suppressible = {
        contract.target_table
        for contract in contracts
        if contract.targets_a_fact
        and contract.suppression is not None
        and _maps_an_additive_measure(contract)
    }
    return tuple(
        withheld_table(table)
        for table in tables
        if table.name in suppressible and not table.name.endswith(WITHHELD_SUFFIX)
    )


def _maps_an_additive_measure(contract: DataContract) -> bool:
    """Whether the contract delivers a measure a residual can carry, one not a distinct count.

    A measure is a delivered column outside the grain. A literal or a load-assigned column is
    authored by us, so it is never a figure AMC could withhold. Nor is an identity column the
    load resolves through the source pack: pack 3 delivers its campaign's ad product outside its
    campaign grain (the ad product follows from the campaign), and that is a label, not a figure.
    """
    return any(
        not field.non_additive
        and not field.is_literal
        and not field.assigned_by_load
        and field.target_column not in contract.grain
        and field.target_column not in _IDENTITY_COLUMNS
        for field in contract.fields
    )


_IDENTITY_COLUMNS = frozenset(
    {
        "marketplace_key",
        "ecosystem_key",
        "platform_key",
        "channel_group_key",
        "channel_key",
        "ad_product_key",
        "campaign_key",
    }
)
"""Conformed identity columns the load resolves through the source pack, never a measure."""
