# SPDX-License-Identifier: MIT-0
"""The curated column list a contract produces, and how each column's type is decided.

`make validate-contracts` checks that a contract's *sources* are real. This module answers
the other half: what table does the contract actually produce. It is what the Iceberg DDL,
the Pydantic models and the DQDL ruleset are generated from, so that a curated schema
exists in exactly one place — the contract — rather than being hand-written alongside it
and drifting (docs/capabilities/data-layer/low-level-design.md §6).

The governing rule is **read what a source states, declare what no source states.** AMC
publishes a type for every column and `contracts/shared/amc-columns.yaml` records it
`[verified]`, so re-declaring those types in each contract would create a second copy that
can disagree with the registry. Everything else — vendor fields, derived values, literals,
the extraction-option columns — has no such source and must be declared.

One resolution rule is a correctness property rather than a convenience, and it is the
reason this module exists at all: **a monetary column's target type is never its source
type.** `impression_cost` is a `LONG` in AMC and becomes a decimal the moment the divisor
is applied. Emitting it as a long would truncate every fee to zero, silently, and the
totals would still look plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domain.data_contracts.contract import DataContract, FieldMapping

# Thresholds that lift a query's bar from 2 distinct users per row to 100. Any output
# column in this set changes what the contract must declare.
ESCALATING_THRESHOLDS = frozenset({"MEDIUM", "HIGH"})

# Never legal in a final SELECT, whatever else a contract says.
NON_OUTPUTTABLE_THRESHOLDS = frozenset({"VERY_HIGH", "INTERNAL"})

BASIS_KEY = "basis_key"
SOURCE_TIMEZONE = "source_timezone"
ECOSYSTEM_KEY = "ecosystem_key"
PLATFORM_KEY = "platform_key"

MEASUREMENT_PROVENANCE_COLUMNS = (BASIS_KEY, SOURCE_TIMEZONE)
BUSINESS_CONTEXT_COLUMNS = (ECOSYSTEM_KEY, PLATFORM_KEY)
CONFORMED_PROVENANCE_COLUMNS = (
    *MEASUREMENT_PROVENANCE_COLUMNS,
    *BUSINESS_CONTEXT_COLUMNS,
)
"""Contract-level columns every fact carries.

Measurement provenance qualifies arithmetic and therefore never belongs on a dimension. Ecosystem
and platform identify customer-facing business context; facts receive them from contract metadata,
while a governed entity dimension may declare them as attributes of the entity it describes.
"""


class CanonicalType(StrEnum):
    """The curated type vocabulary. Values are the **Athena/Glue** type names.

    Deliberately small. A wider vocabulary invites a column typed for how it arrives
    rather than for what it means, and the arriving type is frequently wrong for storage —
    see the monetary rule in the module docstring.

    **Not the Iceberg names, which this docstring claimed until a real deploy said otherwise.**
    Iceberg's primitive is `long` where Athena's is `bigint`, and Glue rejects the create with
    "Cannot parse type string to primitive: bigint". These are the names a human writes in a
    query and reads in `SHOW CREATE TABLE`, so they stay; the Iceberg spelling is applied by
    `infra/lib/constructs/data-lake/iceberg-table.ts`, which is the only consumer that needs
    it. Every other consumer — the row models, the DQDL ruleset — wants a name that means
    something in the query engine.
    """

    BOOLEAN = "boolean"
    INT = "int"
    BIGINT = "bigint"
    DOUBLE = "double"
    DECIMAL = "decimal(38,4)"
    """The only decimal precision used. It is the one Amazon publishes for attributed
    sales, and every monetary column lands here so that scaled and unscaled money share
    one storage type."""

    DATE = "date"
    TIMESTAMP = "timestamp"
    STRING = "string"


# AMC's published types to ours. Anything absent from this map is a type we have never
# seen on a mapped column, and it fails loudly rather than defaulting to STRING — a
# silent default here would store an array or a struct as text.
AMC_TYPE_MAP = {
    "BOOLEAN": CanonicalType.BOOLEAN,
    "INTEGER": CanonicalType.INT,
    "LONG": CanonicalType.BIGINT,
    "DOUBLE": CanonicalType.DOUBLE,
    "DECIMAL": CanonicalType.DECIMAL,
    "DECIMAL(38,4)": CanonicalType.DECIMAL,
    "DATE": CanonicalType.DATE,
    "TIMESTAMP": CanonicalType.TIMESTAMP,
    "STRING": CanonicalType.STRING,
}


@dataclass(frozen=True, slots=True)
class RegisteredColumn:
    """Describe what the AMC column registry knows about one source column."""

    name: str
    table: str
    threshold: str
    source_type: str | None = None
    """AMC's own published type, verbatim. The input to type resolution."""

    disputed: bool = False
    monetary: bool = False
    scale_divisor: int | None = None
    deprecated_for_use: bool = False
    preferred_alternative: str | None = None
    kind: str | None = None
    """AMC's `METRIC` or `DIMENSION`, verbatim, or None where the page publishes no kind (ARP)."""

    @property
    def is_metric(self) -> bool:
        """Whether AMC publishes this column as a metric, the only kind a pack sums."""
        return self.kind == "METRIC"

    @property
    def escalates(self) -> bool:
        """Whether selecting this column lifts the row bar to 100 distinct users."""
        return self.threshold in ESCALATING_THRESHOLDS

    @property
    def outputtable(self) -> bool:
        """Whether this column may appear in a final SELECT at all."""
        return self.threshold not in NON_OUTPUTTABLE_THRESHOLDS


@dataclass(frozen=True, slots=True)
class ColumnRegistry:
    """The verified AMC column surface, as an allowlist.

    An allowlist rather than a denylist because a column's threshold class cannot be
    inferred from its name — `event_hour_utc` is LOW while `event_dt_utc` is MEDIUM — so
    an unrecognised column is unverified, and an unverified column may not be used.
    """

    columns: dict[tuple[str, str], RegisteredColumn]
    """Keyed by (table, column)."""

    known_tables: frozenset[str]
    inherits: dict[str, str]
    """Table to the table whose columns it also resolves against.

    `dsp_video_events_feed` documents only its 15 video columns while carrying the whole
    `dsp_impressions` surface, so resolving against its own page alone would reject 105
    legitimate columns.
    """

    def lookup(self, *, table: str, column: str) -> RegisteredColumn | None:
        """Find a column on a table, following one level of inheritance."""
        found = self.columns.get((table, column))
        if found is not None:
            return found
        parent = self.inherits.get(table)
        if parent is not None:
            return self.columns.get((parent, column))
        return None


@dataclass(frozen=True, slots=True)
class TargetColumn:
    """One column of the curated table, with everything a generator needs.

    This is the hand-off to `adapters/jobs/schema_generation`: Iceberg DDL, Pydantic models
    and DQDL rules are all projections of this list.
    """

    name: str
    type: CanonicalType
    nullable: bool
    provenance: str
    """How the value comes to exist, for the generated comment. A schema a human reads
    without the contract open should still say where each column came from."""

    null_meaning: str | None = None
    """Which `NullMeaning` applies. Reaches the generated DQDL, because "not
    applicable under these options" and "suppressed" need different rules."""


class SchemaError(Exception):
    """A contract's target schema cannot be determined."""


def resolve_target_type(
    mapping: FieldMapping, registered: RegisteredColumn | None
) -> CanonicalType | None:
    """Decide one column's curated type, or None when nothing states it.

    Order matters and each step earns its place:

    1. **Declared wins.** An explicit `target_type` is a decision someone made.
    2. **Monetary is always decimal.** The divisor changes the type, so the source type is
       actively wrong here — this is checked before the registry rather than after.
    3. **The registry, for an AMC source column.** Amazon publishes it and we recorded it.
    4. **Nothing.** Returned as None so the caller can report *which* column, rather than
       raising on the first one and hiding the rest.
    """
    if mapping.target_type is not None:
        return CanonicalType(mapping.target_type)
    if mapping.monetary:
        return CanonicalType.DECIMAL
    if registered is not None and registered.source_type is not None:
        return AMC_TYPE_MAP.get(registered.source_type.upper())
    return None


def _provenance_of(mapping: FieldMapping) -> str:
    """Describe where a column's value comes from, for the generated schema comment."""
    if mapping.is_literal:
        return f"literal {mapping.literal_value!r}"
    if mapping.assigned_by_load:
        return "assigned by the load"
    if mapping.is_derived:
        return f"derived from {', '.join(mapping.derived_from)}"
    if mapping.from_extraction_option:
        return f"extraction option, delivered as {mapping.source_path}"
    return mapping.source_path


def target_schema(contract: DataContract, registry: ColumnRegistry) -> tuple[TargetColumn, ...]:
    """Build the full curated column list for one contract.

    Raises rather than returning findings, unlike `validation.py`. By the time a generator
    runs, `make validate-contracts` has already passed — so an unresolvable type here is a
    programming error rather than an authoring mistake, and emitting a partial schema would
    be worse than failing.
    """
    columns: list[TargetColumn] = []
    unresolved: list[str] = []

    for mapping in contract.fields:
        registered = (
            next(
                (
                    found
                    for table in contract.source_tables
                    if (found := registry.lookup(table=table, column=_bare(mapping.source_path)))
                    is not None
                ),
                None,
            )
            if mapping.resolves_against_registry
            else None
        )
        resolved = resolve_target_type(mapping, registered)
        if resolved is None:
            unresolved.append(mapping.target_column)
            continue
        columns.append(
            TargetColumn(
                name=mapping.target_column,
                type=resolved,
                nullable=mapping.nullable,
                provenance=_provenance_of(mapping),
                null_meaning=mapping.null_meaning.value if mapping.null_meaning else None,
            )
        )

    if unresolved:
        raise SchemaError(
            f"{contract.contract_id}: no type could be resolved for "
            f"{', '.join(unresolved)}. Declare target_type on each, or add the source "
            "column to the registry with its published type."
        )
    return tuple(columns) + conformed_provenance(contract)


def conformed_provenance(contract: DataContract) -> tuple[TargetColumn, ...]:
    """Build the provenance columns every fact carries, or nothing for a dimension.

    A dimension is excluded deliberately rather than incidentally. `dim_product` declares
    a `measurement_basis` because every contract must, but a product is not a measurement
    — it has no basis to reconcile and no day bucket to have been aggregated into. Giving
    it a `basis_key` would invite a join that means nothing.

    A contract that declares a column of either name in its own `fields` is refused rather
    than silently overridden, because two columns of one name is not a table and the
    contract author clearly believed they were supplying it.
    """
    if not contract.targets_a_fact:
        return ()

    declared = {mapping.target_column for mapping in contract.fields}
    collision = declared.intersection(CONFORMED_PROVENANCE_COLUMNS)
    if collision:
        raise SchemaError(
            f"{contract.contract_id}: {', '.join(sorted(collision))} is projected onto "
            "every fact from contract-level metadata, so a field mapping must not also "
            "declare it. Remove the mapping; the contract already states the value."
        )

    return (
        TargetColumn(
            name=BASIS_KEY,
            type=CanonicalType.STRING,
            nullable=False,
            provenance=f"literal {contract.measurement_basis.value!r}",
        ),
        TargetColumn(
            name=SOURCE_TIMEZONE,
            type=CanonicalType.STRING,
            nullable=False,
            provenance=f"literal {contract.source_timezone!r}",
        ),
        TargetColumn(
            name=ECOSYSTEM_KEY,
            type=CanonicalType.STRING,
            nullable=False,
            provenance=f"governed ecosystem {contract.ecosystem_key!r}",
        ),
        TargetColumn(
            name=PLATFORM_KEY,
            type=CanonicalType.STRING,
            nullable=False,
            provenance=f"governed platform {contract.platform_key!r}",
        ),
    )


def conformed_provenance_values(contract: DataContract) -> dict[str, str]:
    """Give the value each conformed provenance column takes on every row of one contract.

    Deliberately adjacent to `conformed_provenance`, which declares the columns. Splitting
    declaration from stamping across two modules is what would let a column be added to the
    schema with nothing ever writing it — which is the defect this pair exists to correct,
    since `basis_key` was declared in the canonical model and written by nothing.

    Constant per contract, not per row. A fact table assembled from several contracts still
    varies per row, because each contract stamps its own values.
    """
    if not contract.targets_a_fact:
        return {}
    return {
        BASIS_KEY: contract.measurement_basis.value,
        SOURCE_TIMEZONE: contract.source_timezone,
        ECOSYSTEM_KEY: contract.ecosystem_key,
        PLATFORM_KEY: contract.platform_key,
    }


def _bare(source_path: str) -> str:
    """Extract the bare column name from a source path."""
    return source_path.rsplit(".", maxsplit=1)[-1].replace("[]", "")
