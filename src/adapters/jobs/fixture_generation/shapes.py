# SPDX-License-Identifier: MIT-0
"""Build source-shaped fixtures from a contract.

Fixtures are **source-shaped, not target-shaped**. A reader's job is to parse what Amazon
delivers, so a fixture that already looks like the curated table skips the only step under
test.

Generated from the contracts rather than hand-authored, because a hand-written fixture is a
third source of truth alongside the contract and the canonical model: when the contract
changes the fixture silently does not, and a test keeps passing against a shape that no
longer exists. Reading the contract means a contract change breaks generation loudly.

**Monetary values are generated in the scaled space.** `total_cost` arrives as millicents,
so a fixture holding `12.34` would let a missing or wrong divisor pass unnoticed. Holding
`1234000` means the divisor is exercised on every load. This is the most important property
of these fixtures, because a wrong divisor is a silent order-of-magnitude error that still
reconciles at the headline level.

Deterministic throughout: the same contract produces the same bytes, so fixtures are
diffable and committable, and a diff means a contract changed.

Types are not resolved here. `domain.data_contracts.schema.target_schema` owns that,
including the monetary rule, and asking it rather than re-deriving keeps one answer to what
a column's type is.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

from adapters.jobs.fixture_generation.catalog import (
    FIRST_DAY,
    CampaignFixturePopulation,
    GovernedValueProvider,
    SyntheticEntityCatalog,
)
from domain.data_contracts.contract import (
    DataContract,
    FieldMapping,
    NullMeaning,
    SourceFormat,
)
from domain.data_contracts.schema import CanonicalType, ColumnRegistry, target_schema
from domain.measurement.lenses import LENS_DEFAULTS
from domain.shared_kernel.values import FOUR_PLACES

DEFAULT_ROWS = 37
"""The row count of a catalog-less or explicitly small unit build: 37 days from its first day,
which crosses a month boundary. The committed fixtures do not use it: `make generate-fixtures`
passes the catalog's `span_days` (design §2.2)."""

MARKETPLACES = ("ATVPDKIKX0DER", "A1F83G8C2ARO7P")
"""Public marketplace identifiers used only when no governed synthetic catalog is supplied."""

AD_PRODUCT_TYPE = "ad_product_type"
AD_PRODUCT_TYPES = (
    "sponsored_products",
    "sponsored_brands",
    "sponsored_display",
    "sponsored_television",
)
"""The closed set `sponsored_ads_traffic.ad_product_type` takes, from docs/sources/amc.md §4.

`NULL` there means a DSP row, which this fixture never produces — the DSP contracts stamp
`amazon_dsp` as a literal instead of reading this column.
"""
"""US and UK. Two, so a fixture exercises the marketplace grain. Both are public
marketplace identifiers, not customer data."""

MANIFEST_PREFIX = "manifest."

"""Matches CanonicalType.DECIMAL, so an unscaled amount is generated at the precision it
will be stored at rather than at one a load would silently round."""


class FixtureError(Exception):
    """A contract's fixture shape cannot be determined."""


@dataclass(frozen=True, slots=True)
class FixtureBuildOptions:
    """Optional fixture row count and governed fictional identity catalog."""

    rows: int = DEFAULT_ROWS
    entity_catalog: SyntheticEntityCatalog | None = None
    campaign_population: CampaignFixturePopulation = CampaignFixturePopulation.VISIBLE
    result: ResultShape | None = None
    """With a result shape, a flat fixture is laid out as that governed pack's AMC result."""

    @property
    def first_day(self) -> date:
        """The first fixture day: the catalog's declared span start, or FIRST_DAY without one."""
        if self.entity_catalog is None:
            return FIRST_DAY
        return self.entity_catalog.shape.fixture_span.first_day

    def __post_init__(self) -> None:
        if self.rows < 1:
            raise FixtureError("a generated fixture requires at least one row.")
        if (
            self.campaign_population is CampaignFixturePopulation.BULK
            and self.entity_catalog is None
        ):
            raise FixtureError("bulk fixture generation requires the governed entity catalog.")


WINDOW_COLUMNS = ("time_window_start", "time_window_end")
"""The window every governed pack stamps from `BUILT_IN_PARAMETER` (`validate_pack`)."""


@dataclass(frozen=True, slots=True)
class ResultShape:
    """How an AMC result for one governed pack is laid out, from verified sources only.

    `columns` is the pack's `outputColumns` and `expected_bar` its `expectedBar`. The trailing
    order and timestamp format are the registry's `suppression_output` block, observed live on
    2026-10-08: the three option columns follow every selected column, and an unsuppressed row's
    user count is the query's bar, not its reach.
    """

    columns: tuple[str, ...]
    expected_bar: int
    trailing_order: tuple[str, ...]
    timestamp_format: str


@dataclass(frozen=True, slots=True)
class Fixture:
    """One generated source artefact, plus the manifest that must accompany it.

    `rows` carries CSV and JSONL records; `document` carries a nested vendor report. Never
    both — which one is populated follows from the contract's `source_format`.
    """

    contract_id: str
    source_format: SourceFormat
    manifest: dict[str, Any]
    document: dict[str, Any] | None = None
    rows: tuple[dict[str, Any], ...] = ()

    @property
    def extension(self) -> str:
        """The file extension matching this fixture's source format."""
        return {
            SourceFormat.CSV: "csv",
            SourceFormat.JSON_DUAL_ARRAY: "json",
            SourceFormat.JSONL: "jsonl",
        }[self.source_format]


def _delivered_name(mapping: FieldMapping) -> str:
    """Give the column name as it appears in the delivered file.

    For a plain source column it is the last path segment. A derived value or an
    extraction-option column has no source name, so the convention is that the extraction
    aliases it to the canonical column name — which is also what makes the reader trivial.
    It is a convention the extraction SQL must honour, not something the delivered file can
    tell us; docs/capabilities/data-layer/low-level-design.md §6 (Delivered column names)
    records it.
    """
    if mapping.is_derived or mapping.from_extraction_option:
        return mapping.target_column
    return mapping.source_path.rsplit(".", maxsplit=1)[-1].replace("[]", "")


PARENTS = 3
"""How many rows share a parent ASIN. Gives more than one parent across DEFAULT_ROWS, so a
hierarchy fixture can exercise roll-up instead of pairing every child with itself."""


def _column_offset(name: str) -> int:
    """Give a column a stable numeric identity, so no two columns hold the same value.

    Two columns holding identical values is not a cosmetic problem, because analysis is
    mostly differences between columns. `total_cost` and `impression_cost` colliding makes
    their difference zero, and that difference is the fee — so a fixture built to prove fees
    survive the divisor would contain no fees at all. `shippedRevenue` colliding with
    `shippedCogs` makes every margin zero for the same reason.

    What this does **not** do is respect relationships between columns: nothing here makes a
    total exceed its own component, because no contract declares which column is a component
    of which. Any test needing that relationship needs a hand-built fixture.

    `hashlib` rather than `hash()` because `hash()` on a string is salted per process, and
    these fixtures are committed: a non-deterministic value would produce a diff on every
    run and `make check-fixtures` would never pass twice.
    """
    digest = hashlib.sha256(name.encode()).digest()
    return digest[0] % 89 + 1


def _day(first_day: date, index: int) -> str:
    """Give the fixture's date at one row index, crossing month boundaries as needed."""
    return (first_day + timedelta(days=index)).isoformat()


CENTS = Decimal("0.13")
"""Turns a column's offset into an amount with real decimal places, so rounding is
exercised rather than every amount landing on a whole unit."""

_SCALARS: dict[CanonicalType, Callable[[int, int], Any]] = {
    # Alternating rather than constant, so both branches are exercised.
    CanonicalType.BOOLEAN: lambda row, _offset: row % 2 == 0,
    CanonicalType.INT: lambda row, offset: (row + 1) * offset,
    CanonicalType.BIGINT: lambda row, offset: (row + 1) * offset,
    CanonicalType.DOUBLE: lambda row, offset: float((row + 1) * offset),
    CanonicalType.DECIMAL: lambda row, offset: str(
        (Decimal(row + 1) * Decimal(offset) * CENTS).quantize(FOUR_PLACES)
    ),
}


def _source_money(entry: _Field, amount: Decimal) -> str | int:
    """Encode a canonical amount in the scaling space declared by its source mapping."""
    divisor = entry.mapping.scale_divisor or 1
    if divisor > 1:
        return int(amount * divisor)
    return str(amount.quantize(FOUR_PLACES))


def _money(entry: _Field, row: int, offset: int) -> str | int | float:
    """Produce a structural amount in the space and JSON type the source ships."""
    amount = Decimal(row + 1) * Decimal(offset) * CENTS
    if entry.mapping.source_path.startswith(("salesByAsin[]", "salesAggregate[]")):
        return float(amount.quantize(FOUR_PLACES))
    return _source_money(entry, amount)


def _string(entry: _Field, row: int, provider: GovernedValueProvider | None = None) -> str:
    """Produce a fictional string, preferring the governed source-invariant catalog."""
    delivered = entry.delivered_name.lower()
    name = entry.mapping.target_column
    value = None
    if name in LENS_DEFAULTS:
        value = LENS_DEFAULTS[name]
    elif provider is not None:
        value = provider.string_value(entry.delivered_name, name, row)
    if value is None and name == "channel_group_key":
        ad_product = AD_PRODUCT_TYPES[row % len(AD_PRODUCT_TYPES)]
        value = (
            "search"
            if ad_product in {"sponsored_products", "sponsored_brands"}
            else "video"
            if ad_product == "sponsored_television"
            else "display"
        )
    elif value is None and entry.mapping.value_domain:
        value = entry.mapping.value_domain[row % len(entry.mapping.value_domain)]
    if value is None and (delivered.endswith("asin") or name.endswith("asin")):
        value = f"B0PARENT{row // PARENTS:02d}" if "parent" in delivered else f"B0FIXTUR{row:02d}"
    elif value is None and delivered == AD_PRODUCT_TYPE:
        # Source ad products are schema-derived lexical exercise, not a distribution claim.
        value = AD_PRODUCT_TYPES[row % len(AD_PRODUCT_TYPES)]
    elif value is None and "currency" in name:
        value = "USD"
    elif value is None:
        value = {
            "campaign_country": "US",
            "marketplace_name": "AMAZON.COM",
        }.get(delivered)
    if value is None and "marketplace" in name:
        # One delivery declares one marketplace; rows cannot silently span populations.
        value = MARKETPLACES[0]
    return value if value is not None else f"{name}-{row:03d}"


def _scenario_number(
    entry: _Field,
    row: int,
    provider: GovernedValueProvider,
) -> str | int | float:
    """Encode one declared scenario measurement in its delivered source type."""
    value = provider.numeric_value(entry.mapping.target_column, row)
    if entry.mapping.monetary:
        if entry.mapping.source_path.startswith(("salesByAsin[]", "salesAggregate[]")):
            return float(Decimal(value).quantize(FOUR_PLACES))
        return _source_money(entry, Decimal(value))
    if entry.canonical is CanonicalType.DECIMAL:
        return str(Decimal(value).quantize(FOUR_PLACES))
    if entry.canonical is CanonicalType.DOUBLE:
        return float(value)
    return int(value)


def _value_for(entry: _Field, row: int, provider: GovernedValueProvider | None = None) -> Any:
    """Produce one deterministic value in the shape the *source* delivers it."""
    offset = _column_offset(entry.mapping.target_column)
    logical_row = provider.day_index(row) if provider is not None else row

    # A generated delivery is a clean baseline, not an invented suppression distribution.
    value: Any = None
    if entry.mapping.from_extraction_option and entry.mapping.target_column == "rows_suppressed":
        value = False
    elif (
        entry.mapping.from_extraction_option and entry.mapping.target_column == "suppression_reason"
    ):
        value = None
    elif provider is not None and (
        entry.mapping.monetary
        or entry.canonical
        in {
            CanonicalType.INT,
            CanonicalType.BIGINT,
            CanonicalType.DOUBLE,
            CanonicalType.DECIMAL,
        }
    ):
        value = _scenario_number(entry, row, provider)
    elif entry.mapping.monetary:
        value = _money(entry, row, offset)
    elif entry.canonical is CanonicalType.STRING:
        if provider is not None and entry.delivered_name.lower() == "campaign":
            value = provider.campaign_name(row)
        else:
            value = _string(entry, row, provider)
    elif entry.canonical is CanonicalType.DATE:
        value = (
            _day(entry.first_day, entry.total_rows - 1)
            if "end" in entry.delivered_name.lower() and "[]" not in entry.mapping.source_path
            else _day(entry.first_day, logical_row)
        )
    elif entry.canonical is CanonicalType.TIMESTAMP:
        value = f"{_day(entry.first_day, logical_row)}T{logical_row % 24:02d}:00:00Z"
    else:
        factory = _SCALARS.get(entry.canonical)
        if factory is None:
            raise FixtureError(
                f"{entry.mapping.target_column} is {entry.canonical.value}, which no fixture "
                "value generator covers."
            )
        value = factory(row, offset)
    return value


def _assign(target: dict[str, Any], path: list[str], value: Any) -> None:
    """Write a value into a nested dict, creating intermediate objects and list slots."""
    cursor: Any = target
    for index, segment in enumerate(path):
        last = index == len(path) - 1
        if segment.endswith("]") and "[" in segment:
            key, raw_index = segment[:-1].split("[", maxsplit=1)
            slot = int(raw_index)
            holder = cursor.setdefault(key, [])
            while len(holder) <= slot:
                holder.append(None)
            if last:
                holder[slot] = value
                return
            if holder[slot] is None:
                holder[slot] = {}
            cursor = holder[slot]
            continue
        if last:
            cursor[segment] = value
            return
        cursor = cursor.setdefault(segment, {})


@dataclass(frozen=True, slots=True)
class _Field:
    """Everything generating one column's values depends on.

    Carries `total_rows` because a document-level value can depend on the span of the rows
    beneath it, not only on its own row: an end date has to be the last day covered.
    """

    mapping: FieldMapping
    canonical: CanonicalType
    delivered_name: str
    total_rows: int
    first_day: date


def _requested_but_unmapped(contract: DataContract) -> tuple[FieldMapping, ...]:
    """Build mappings for suppression columns the extraction requests but the table does not store.

    A fixture must contain what the *source* delivers, and AMC delivers all three suppression
    columns whenever the workflow sets their options — whether or not the curated table keeps
    them. A conformed fact keeps only the discriminator (G-28), so without this the generated
    fixture would omit the other two and `check_header` would reject the delivery for evidence
    the extraction options were never set.

    Which is the realistic shape rather than an inconvenience: a delivered file carrying columns
    the contract deliberately ignores is the ordinary case, and no generated fixture exercised it
    before.
    """
    if contract.suppression is None:
        return ()
    stored = {mapping.target_column for mapping in contract.fields}
    return tuple(
        FieldMapping(
            source_path=name,
            target_column=name,
            target_type=CanonicalType.INT.value
            if name == contract.suppression.distinct_user_count_column
            else CanonicalType.STRING.value,
            from_extraction_option=True,
            nullable=True,
            null_meaning=NullMeaning.SUPPRESSED,
        )
        for name in contract.suppression.all_columns
        if name not in stored
    )


def _required_source_siblings(contract: DataContract) -> tuple[FieldMapping, ...]:
    """Emit Vendor fields required by the source grammar but not stored by this contract.

    The contracts correctly map only curated columns; a source-shaped fixture must still
    carry required siblings such as ``endDate`` and every nested Amount currencyCode.
    docs/sources/vendor-central.md §3 owns this source grammar.
    """
    source_only: dict[str, tuple[tuple[str, str, str], ...]] = {
        "vendor-sales-by-asin": (
            ("salesByAsin[].endDate", "fixture_sales_by_asin_end", "date"),
            (
                "salesByAsin[].shippedCogs.currencyCode",
                "fixture_sales_by_asin_cogs_currency",
                "string",
            ),
            (
                "salesByAsin[].orderedRevenue.currencyCode",
                "fixture_sales_by_asin_ordered_currency",
                "string",
            ),
        ),
        "vendor-sales-reported-total": (
            ("salesAggregate[].endDate", "fixture_sales_aggregate_end", "date"),
            (
                "salesAggregate[].shippedCogs.currencyCode",
                "fixture_sales_aggregate_cogs_currency",
                "string",
            ),
            (
                "salesAggregate[].orderedRevenue.currencyCode",
                "fixture_sales_aggregate_ordered_currency",
                "string",
            ),
        ),
        "vendor-traffic-by-asin": (
            ("trafficByAsin[].endDate", "fixture_traffic_by_asin_end", "date"),
        ),
        "vendor-traffic-reported-total": (
            ("trafficAggregate[].endDate", "fixture_traffic_aggregate_end", "date"),
        ),
    }
    return tuple(
        FieldMapping(source_path=path, target_column=target, target_type=type_name)
        for path, target, type_name in source_only.get(contract.contract_id, ())
    )


def _emittable(
    contract: DataContract, registry: ColumnRegistry, span: tuple[date, int]
) -> tuple[_Field, ...]:
    """Select the fields a fixture can carry, with each one's resolved type.

    Two kinds of mapping are dropped, for different reasons:

    A **literal** has no source column at all — it is stamped during mapping — so putting
    one in a fixture would assert the source delivered something it never sends.

    A path that is a strict **prefix** of another mapped path names a whole object rather
    than a scalar. `reportSpecification.reportOptions` becomes `report_options_hash`: the
    reader hashes the object its siblings already materialise. Writing a scalar there would
    overwrite those siblings and produce a document the source could never emit.
    """
    first_day, rows = span
    types = {column.name: column.type for column in target_schema(contract, registry)}
    paths = {m.source_path for m in contract.fields if m.source_path}

    selected: list[_Field] = []
    for mapping in (
        *contract.fields,
        *_requested_but_unmapped(contract),
        *_required_source_siblings(contract),
    ):
        if mapping.has_no_delivered_value:
            continue
        if any(other.startswith(f"{mapping.source_path}.") for other in paths):
            continue
        canonical = types.get(mapping.target_column) or (
            CanonicalType(mapping.target_type) if mapping.target_type else None
        )
        if canonical is None:
            raise FixtureError(
                f"{contract.contract_id}: {mapping.target_column} has no resolved type, "
                "so no value can be generated for it."
            )
        selected.append(
            _Field(
                mapping=mapping,
                canonical=canonical,
                delivered_name=_delivered_name(mapping),
                total_rows=rows,
                first_day=first_day,
            )
        )
    return tuple(selected)


def _result_rows(
    contract: DataContract, rows: tuple[dict[str, Any], ...], options: FixtureBuildOptions
) -> tuple[dict[str, Any], ...]:
    """Lay flat rows out as the governed pack's AMC result: its columns, then the live trailer.

    Refuses a pack whose columns aren't exactly what the contract delivers plus the stamped
    window, so a pack edit breaks generation instead of producing a fixture no pack returns.
    """
    result = options.result
    if result is None:
        return rows
    trailer = result.trailing_order
    header = (*(name for name in result.columns if name not in trailer), *trailer)
    delivered: set[str] = set(rows[0]) if rows else set()
    produced = delivered | set(WINDOW_COLUMNS)
    if rows and set(header) != produced:
        raise FixtureError(
            f"{contract.contract_id}: the pack's outputColumns disagree with the generated "
            f"columns; missing={sorted(set(header) - produced)}, "
            f"extra={sorted(produced - set(header))}."
        )
    start = datetime.combine(options.first_day, time(), tzinfo=UTC)
    days = options.rows
    stamped = {
        WINDOW_COLUMNS[0]: start.strftime(result.timestamp_format),
        WINDOW_COLUMNS[1]: (start + timedelta(days=days)).strftime(result.timestamp_format),
    }
    user_count = contract.suppression.distinct_user_count_column if contract.suppression else None
    discriminator = contract.suppression.discriminator_column if contract.suppression else None
    laid_out: list[dict[str, Any]] = []
    for row in rows:
        values = {**row, **stamped}
        if user_count is not None and discriminator is not None and values[discriminator] is False:
            values[user_count] = result.expected_bar
        laid_out.append({name: values[name] for name in header})
    return tuple(laid_out)


def build(
    contract: DataContract,
    registry: ColumnRegistry,
    options: FixtureBuildOptions | None = None,
) -> Fixture:
    """Generate one schema-derived fixture with optional governed fictional identities."""
    selected = options or FixtureBuildOptions()
    rows = selected.rows
    fields = _emittable(contract, registry, (selected.first_day, rows))
    in_document = tuple(f for f in fields if not f.mapping.source_path.startswith(MANIFEST_PREFIX))
    provider = (
        GovernedValueProvider(
            contract=contract,
            catalog=selected.entity_catalog,
            population=selected.campaign_population,
        )
        if selected.entity_catalog is not None
        else None
    )

    if contract.source_format is SourceFormat.JSON_DUAL_ARRAY:
        physical_rows = (
            rows * provider.product_count
            if provider is not None and provider.product_bearing
            else rows
        )
        document = _build_document(in_document, physical_rows, provider)
        return Fixture(
            contract_id=contract.contract_id,
            source_format=contract.source_format,
            manifest=_declaration(contract, selected, document),
            document=document,
        )

    # CSV and JSONL are both flat and one record per row. They differ only in how they are
    # serialised, which is the writer's concern rather than the shape's.
    physical_rows = (
        rows * provider.campaign_count
        if provider is not None and provider.campaign_bearing
        else rows
    )
    rows_out = tuple(
        {field.delivered_name: _value_for(field, row, provider) for field in in_document}
        for row in range(physical_rows)
        if provider is None or provider.include_row(row)
    )
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=_declaration(contract, selected),
        rows=_result_rows(contract, rows_out, selected),
    )


def declaration_for(contract: DataContract, options: FixtureBuildOptions) -> dict[str, Any]:
    """Give a flat fixture's landing declaration, for a builder that lays out its own rows."""
    return _declaration(contract, options)


def _declaration(
    contract: DataContract,
    options: FixtureBuildOptions,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the landing declaration that accompanies a generated fixture.

    **A declaration, not a manifest.** It carries only what a producer knows before it writes
    anything: the dataset, the options, the window and the watermark. Everything measured from
    the bytes — `option_set_hash`, `declared_columns`, `objects`, the checksum — is computed by
    `adapters/jobs/curation/manifests.build_manifest` when the delivery is landed. A committed
    checksum would rot the moment a fixture was regenerated, and committed columns would make
    the header assertion vacuous.

    Every field is derived from the same values the rows are generated from, so the document and
    the description of it cannot drift. That was the defect this replaced: the old shape carried
    `contract_id`, `contract_version` and `row_count` — none of which a landing declaration has a
    use for — and none of `marketplace`, `period_start`, `period_end` or `source_watermark`, all
    of which are required. It also named the contract, which [ADR-030] deliberately keeps out of
    landing: a delivery is keyed on its dataset, and one dataset can feed two contracts.
    """
    rows = options.rows
    catalog = options.entity_catalog
    first_day = options.first_day
    source_scopes = {
        "amc": {"namespace": "amc.entity", "value": "fictional-fixture-amc-entity"},
        "vendor_central": {
            "namespace": "vendor_central.vendor_group",
            "value": "fictional-fixture-vendor-group",
        },
        "vendor_data_kiosk": {
            "namespace": "vendor_data_kiosk.vendor_group",
            "value": "fictional-fixture-vendor-group",
        },
    }
    try:
        source_scope = source_scopes[contract.source_system]
    except KeyError as error:
        raise FixtureError(
            f"{contract.contract_id}: no fictional source scope is governed for "
            f"{contract.source_system}."
        ) from error
    return {
        "$comment": (
            "GENERATED landing declaration. Provenance only — the manifest that reaches the "
            "raw zone is built from the bytes at landing time. See docs/concepts/"
            "provenance-chain.md section 2.5 and ADR-030."
        ),
        "source_system": contract.source_system,
        "source_dataset": contract.source_dataset,
        "source_scope": source_scope,
        "marketplace": (
            catalog.marketplace_aliases["amazon.marketplace_id"]
            if catalog is not None
            else MARKETPLACES[0]
        ),
        "option_set": _option_set(document),
        "period_start": _day(first_day, 0),
        "period_end": _day(first_day, rows - 1),
        # Data Kiosk hierarchy exposes current state and no source business revision. Its V2
        # source pack orders snapshots by immutable system-first-observed evidence instead.
        "source_watermark": (
            None
            if contract.contract_id == "vendor-product-hierarchy"
            else _day(first_day, rows - 1)
        ),
        # A pure function of the declared span (ADR-086): the start of the last fixture day, so
        # every landing commit (a new last_day) gets a new prefix and regeneration stays
        # byte-identical. Dev-only: a real delivery could not report a day before it ended.
        "retrieved_at": f"{_day(first_day, rows - 1)}T00:00:00Z",
    }


def _option_set(document: dict[str, Any] | None) -> dict[str, Any]:
    """Read the extraction options a delivery was pulled under, or state that it has none.

    A vendor report carries them in `reportSpecification.reportOptions`, and they are part of
    dataset identity — the same report type under two `distributorView` values is two different
    datasets, which is why `report_options_hash` is in those contracts' partition keys.

    **AMC has none, and empty is the honest answer rather than a gap.** No AMC contract carries
    `report_options_hash` in its partition keys, so nothing the producer chose participates in
    the identity of an AMC delivery. Inventing a key to fill the field would be fabricating
    provenance.
    """
    if document is None:
        return {}
    specification = document.get("reportSpecification")
    if not isinstance(specification, dict):
        return {}
    options = cast("dict[str, Any]", specification).get("reportOptions")
    return cast("dict[str, Any]", options) if isinstance(options, dict) else {}


def _build_document(
    fields: tuple[_Field, ...],
    rows: int,
    provider: GovernedValueProvider | None = None,
) -> dict[str, Any]:
    """Assemble a nested vendor report from the contract's own source paths.

    A vendor document carries two grains at once: `reportSpecification` describes the whole
    report, `salesByAsin[]` repeats per row. That split is the reason a source path is a
    path rather than a name, and it is read straight off the path here.
    """
    document: dict[str, Any] = {}
    for entry in fields:
        path = entry.mapping.source_path
        if "[]" in path:
            array_name, remainder = path.split("[].", maxsplit=1)
            holder = document.setdefault(array_name, [])
            while len(holder) < rows:
                holder.append({})
            for row in range(rows):
                _assign(
                    holder[row],
                    remainder.split("."),
                    _value_for(entry, row, provider),
                )
            continue
        # Document-level: one value for the whole report, not one per row.
        _assign(document, path.split("."), _value_for(entry, 0, provider))
    return document
