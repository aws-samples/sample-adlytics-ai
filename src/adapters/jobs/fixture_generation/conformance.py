# SPDX-License-Identifier: MIT-0
"""Hold every committed AMC fixture to the verified sources it claims to be shaped like.

A generated fixture is built from our own declarations, so on its own it proves only that the
generator agrees with itself. This check ties it to three sources that don't come from the
generator:

- the governed query pack, through the producer's own `validate_result`, so a fixture is
  refused by exactly the check a real AMC result is refused by;
- the column registry, for every source column's presence, published type and evidence
  status. A `pending` or `disputed` column may feed a fixture only when the `conformance`
  section of `contracts/shared/fixture-provenance.yaml` marks it, and every applied marker is
  reported;
- the live suppression shape in the registry's `suppression_output` block (observed
  2026-10-08), for the option columns' order and every row's suppression markers. That shape
  is what one scope of live runs returned, not AMC's general format (the block states the
  scope), so holding a fixture to it tests the fixture, not AMC;
- AMC's CSV escape (`adapters.amc_csv`): a fixture spelling a quote RFC 4180's way, or with a
  backslash AMC isn't known to write, is `CSV_ESCAPE_AMBIGUOUS` and nothing else is checked.

A hand-built trap encodes a behaviour on purpose, so it isn't held to the generated shape. Its
suppression markers are compared with the live shape all the same, and the exact set of
differences must equal the set its declaration names, so a trap can't drift unnoticed.

Every finding carries a stable code and an exact detail string, so a caller and a test can
assert the precise refusal.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, cast

from adapters.amc_csv import dict_rows, escape_finding, read_rows, write_rows
from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.fixture_generation.cohorts import COHORT_CONTRACTS, COHORT_STANDARD
from adapters.jobs.fixture_generation.daily import DAILY_BOUNDS, DailyBound
from adapters.jobs.fixture_generation.exposure import EXPOSURE_CONTRACT, PathReason
from adapters.jobs.fixture_generation.frequency import FREQUENCY_CONTRACTS
from adapters.jobs.fixture_generation.periodic import BUYER_BOUNDS, BuyerBound
from adapters.jobs.fixture_generation.shapes import WINDOW_COLUMNS, ResultShape
from adapters.outbound import repository_yaml
from adapters.producer.packs import load_query_packs
from adapters.producer.results import parse_csv_result
from domain.data_contracts.contract import DataContract, FieldMapping
from domain.data_contracts.schema import (
    CanonicalType,
    ColumnRegistry,
    RegisteredColumn,
    resolve_target_type,
)
from domain.producer.models import GovernedQueryPack
from domain.producer.validation import validate_result
from domain.semantics.calendar_periods import is_complete_period, vintage_ending
from domain.semantics.time import TimeGrain

REGISTRY_PATH = Path("contracts/shared/amc-columns.yaml")
DECLARATION_PATH = Path("contracts/shared/fixture-provenance.yaml")
DECLARATION_KEY = "conformance"

DISCRIMINATOR = "rows_suppressed"
REASON = "suppression_reason"
USER_COUNT = "distinct_user_count"
MARKABLE_STATUSES = ("pending", "disputed")


class ConformanceError(ValueError):
    """A conformance declaration or the registry's suppression block is malformed."""


@dataclass(frozen=True, slots=True)
class SuppressionOutput:
    """The three suppression option columns as observed live (registry `suppression_output`).

    Scoped to the runs that block names; outside them the reason format is a docs-vs-live
    disagreement (docs/sources/amc.md §5).
    """

    observed: str
    source: str
    trailing_order: tuple[str, ...]
    discriminator_values: tuple[str, ...]
    suppressed_reason_format: str
    timestamp_format: str
    bars: Mapping[str, int | None]
    """Each threshold class's minimum distinct users, from the registry's `threshold_classes`."""

    @property
    def suppressed_value(self) -> str:
        """The discriminator text on a suppressed row."""
        return self.discriminator_values[0]

    def reason(self, *, users: int, table: str, bar: int) -> str:
        """Render the reason a suppressed row carried in the observed runs, for fixtures only."""
        return self.suppressed_reason_format.format(users=users, table=table, bar=bar)


@dataclass(frozen=True, slots=True)
class ColumnMarker:
    """One explicit allowance for a pending or disputed column to feed one contract's fixture."""

    contract_id: str
    column: str
    status: str
    reason: str

    def __str__(self) -> str:
        return f"MARKED {self.status} {self.contract_id}.{self.column}: {self.reason}"


@dataclass(frozen=True, slots=True)
class TrapDeviation:
    """The exact suppression-shape differences one hand-built trap is declared to carry."""

    fixture: str
    contract_id: str
    codes: tuple[str, ...]
    basis: str

    def __str__(self) -> str:
        codes = ", ".join(self.codes) if self.codes else "none"
        return f"TRAP {self.fixture} ({self.contract_id}) differs by {codes}: {self.basis}"


@dataclass(frozen=True, slots=True)
class ConformanceDeclaration:
    """Every column marker and trap deviation the provenance file's `conformance` declares."""

    markers: tuple[ColumnMarker, ...]
    trap_deviations: tuple[TrapDeviation, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    """One conformance refusal: a stable code, the fixture it names, and an exact detail."""

    code: str
    fixture: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} {self.fixture}: {self.detail}"


@dataclass(frozen=True, slots=True)
class FixtureUnderTest:
    """One CSV result fixture with the contract and governed pack it stands for."""

    name: str
    text: str
    contract: DataContract
    pack: GovernedQueryPack
    refused_days: frozenset[date] = frozenset()
    """Days whose delivery the synthetic catalog declares refused (a daily-delivery pack)."""
    refused_periods: frozenset[date] = frozenset()
    """Periodic deliveries the catalog declares the load refuses, by first day (pack 5's
    currency disagreement), which `land_fixtures` never lands."""


@dataclass(frozen=True, slots=True)
class Sources:
    """The verified sources every check reads: the column registry and the live shape."""

    registry: ColumnRegistry
    shape: SuppressionOutput
    pending: frozenset[tuple[str, str]]
    """(table, column) pairs the registry gives a `pending` claim with no accepted result."""


@dataclass(frozen=True, slots=True)
class GeneratedReport:
    """The findings on one generated fixture and the markers that let a column through."""

    findings: tuple[Finding, ...]
    markers: tuple[ColumnMarker, ...]


type RawMapping = dict[str, object]


def _mapping(value: object, label: str) -> RawMapping:
    if not isinstance(value, dict):
        raise ConformanceError(f"{label} must be a mapping.")
    return cast("RawMapping", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConformanceError(f"{label} must be a non-empty string.")
    return value.strip()


def _texts(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConformanceError(f"{label} must be a list.")
    return tuple(_text(item, f"{label}[]") for item in cast("list[object]", value))


def _exact_keys(raw: RawMapping, keys: frozenset[str], label: str) -> None:
    if frozenset(raw) != keys:
        raise ConformanceError(f"{label} takes exactly {sorted(keys)}; found {sorted(raw)}.")


def load_suppression_output(root: Path) -> SuppressionOutput:
    """Read the registry's live suppression shape and its threshold bars."""
    path = root / REGISTRY_PATH
    raw = _mapping(repository_yaml.safe_load(path.read_text()), str(path))
    block = _mapping(raw.get("suppression_output"), "suppression_output")
    _exact_keys(
        block,
        frozenset(
            {
                "observed",
                "source",
                "trailing_order",
                "discriminator_values",
                "suppressed_reason_format",
                "timestamp_format",
            }
        ),
        "suppression_output",
    )
    classes = _mapping(raw.get("threshold_classes"), "threshold_classes")
    bars: dict[str, int | None] = {}
    for name, value in classes.items():
        bar = _mapping(value, f"threshold_classes.{name}").get("min_distinct_users")
        if bar is not None and (isinstance(bar, bool) or not isinstance(bar, int)):
            raise ConformanceError(f"threshold_classes.{name}.min_distinct_users must be an int.")
        bars[name] = bar
    observed = block["observed"]
    observed_text = (
        observed.isoformat() if isinstance(observed, date) else _text(observed, "observed")
    )
    discriminator = _texts(block["discriminator_values"], "discriminator_values")
    if len(discriminator) != 2:  # noqa: PLR2004
        raise ConformanceError("discriminator_values names the suppressed, then the kept, text.")
    return SuppressionOutput(
        observed=observed_text,
        source=_text(block["source"], "source"),
        trailing_order=_texts(block["trailing_order"], "trailing_order"),
        discriminator_values=discriminator,
        suppressed_reason_format=_text(
            block["suppressed_reason_format"], "suppressed_reason_format"
        ),
        timestamp_format=_text(block["timestamp_format"], "timestamp_format"),
        bars=bars,
    )


def load_pending_columns(root: Path) -> frozenset[tuple[str, str]]:
    """Read every (table, column) the registry records a `pending` live claim on."""
    path = root / REGISTRY_PATH
    raw = _mapping(repository_yaml.safe_load(path.read_text()), str(path))
    pending: set[tuple[str, str]] = set()
    for table, value in _mapping(raw.get("tables"), "tables").items():
        columns = _mapping(value, f"tables.{table}").get("columns")
        for column, entry in _mapping(columns or {}, f"tables.{table}.columns").items():
            if isinstance(entry, dict) and "pending" in entry:
                pending.add((table, column))
    return frozenset(pending)


def load_conformance_declaration(root: Path) -> ConformanceDeclaration:
    """Read every column marker and trap deviation, refusing an unknown key or status."""
    path = root / DECLARATION_PATH
    document = _mapping(repository_yaml.safe_load(path.read_text()), str(path))
    raw = _mapping(document.get(DECLARATION_KEY), f"{path}: {DECLARATION_KEY}")
    _exact_keys(raw, frozenset({"marked_columns", "trap_deviations"}), DECLARATION_KEY)
    markers: list[ColumnMarker] = []
    for item in cast("list[object]", raw["marked_columns"] or []):
        entry = _mapping(item, "marked_columns[]")
        _exact_keys(entry, frozenset({"contract", "column", "status", "reason"}), "marker")
        status = _text(entry["status"], "marker status")
        if status not in MARKABLE_STATUSES:
            raise ConformanceError(f"marker status must be one of {list(MARKABLE_STATUSES)}.")
        markers.append(
            ColumnMarker(
                contract_id=_text(entry["contract"], "marker contract"),
                column=_text(entry["column"], "marker column"),
                status=status,
                reason=" ".join(_text(entry["reason"], "marker reason").split()),
            )
        )
    deviations: list[TrapDeviation] = []
    for item in cast("list[object]", raw["trap_deviations"] or []):
        entry = _mapping(item, "trap_deviations[]")
        _exact_keys(entry, frozenset({"fixture", "contract", "codes", "basis"}), "trap deviation")
        deviations.append(
            TrapDeviation(
                fixture=_text(entry["fixture"], "trap fixture"),
                contract_id=_text(entry["contract"], "trap contract"),
                codes=tuple(sorted(_texts(entry["codes"], "trap codes"))),
                basis=" ".join(_text(entry["basis"], "trap basis").split()),
            )
        )
    keys = [(marker.contract_id, marker.column) for marker in markers]
    if len(set(keys)) != len(keys):
        raise ConformanceError("a column is marked twice.")
    traps = [deviation.fixture for deviation in deviations]
    if len(set(traps)) != len(traps):
        raise ConformanceError("a trap is declared twice.")
    return ConformanceDeclaration(markers=tuple(markers), trap_deviations=tuple(deviations))


# --- cell parsing -------------------------------------------------------------------------


def _is_integer(value: str) -> bool:
    digits = value[1:] if value.startswith("-") else value
    return digits.isascii() and digits.isdigit()


def _is_double(value: str) -> bool:
    try:
        return math.isfinite(float(value)) and value.strip() == value
    except ValueError:
        return False


def _is_decimal(value: str) -> bool:
    try:
        return Decimal(value).is_finite() and value.strip() == value
    except InvalidOperation:
        return False


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == len("YYYY-MM-DD")


def _parsers(shape: SuppressionOutput) -> dict[str, Callable[[str], bool]]:
    def is_timestamp(value: str) -> bool:
        try:
            datetime.strptime(value, shape.timestamp_format)  # noqa: DTZ007 - a UTC wire format
        except ValueError:
            return False
        return True

    return {
        "STRING": lambda _value: True,
        "INTEGER": _is_integer,
        "LONG": _is_integer,
        "DOUBLE": _is_double,
        "DECIMAL": _is_decimal,
        "DECIMAL(38,4)": _is_decimal,
        "DATE": _is_date,
        "TIMESTAMP": is_timestamp,
        "BOOLEAN": lambda value: value in shape.discriminator_values,
    }


_CANONICAL_AS_AMC = {
    CanonicalType.STRING: "STRING",
    CanonicalType.INT: "INTEGER",
    CanonicalType.BIGINT: "LONG",
    CanonicalType.DOUBLE: "DOUBLE",
    CanonicalType.DECIMAL: "DECIMAL",
    CanonicalType.DATE: "DATE",
    CanonicalType.TIMESTAMP: "TIMESTAMP",
    CanonicalType.BOOLEAN: "BOOLEAN",
}
"""A derived or option column has no AMC page type, so its contract type is read in AMC terms."""


# --- shared helpers -----------------------------------------------------------------------


def _read(text: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    rows = read_rows(text)
    if not rows:
        return (), ()
    return rows[0], tuple(row for row in rows[1:] if row)


def _escape_findings(name: str, text: str) -> tuple[Finding, ...]:
    """Refuse a fixture whose quoting AMC's reader would refuse, before reading anything."""
    found = escape_finding(text)
    if found is None:
        return ()
    return (
        Finding(found.code, name, f"line {found.line}, column {found.column}: {found.problem}."),
    )


def _fields_by_name(contract: DataContract) -> dict[str, FieldMapping]:
    return {
        field.delivered_name: field for field in contract.fields if not field.has_no_delivered_value
    }


def _registered(
    contract: DataContract, registry: ColumnRegistry, field: FieldMapping
) -> RegisteredColumn | None:
    if not field.resolves_against_registry:
        return None
    for table in contract.source_tables:
        found = registry.lookup(table=table, column=field.delivered_name)
        if found is not None:
            return found
    return None


def _trailing_order(
    fixture: FixtureUnderTest, header: tuple[str, ...], shape: SuppressionOutput
) -> tuple[Finding, ...]:
    expected = shape.trailing_order
    actual = header[-len(expected) :]
    if actual == expected:
        return ()
    return (
        Finding(
            "FIXTURE_TRAILING_ORDER",
            fixture.name,
            f"the last {len(expected)} columns are {list(actual)}; AMC delivers {list(expected)}.",
        ),
    )


@dataclass(frozen=True, slots=True)
class _Tally:
    """Rows breaking one rule: how many, and the first one's 1-based data-row number and text."""

    count: int = 0
    first: str = ""

    def add(self, row_number: int, text: str) -> _Tally:
        return _Tally(self.count + 1, self.first or f"first at data row {row_number}: {text}")


def check_suppression(fixture: FixtureUnderTest, sources: Sources) -> tuple[Finding, ...]:
    """Compare the option columns' order and every row's suppression markers with the live shape.

    On an unsuppressed row the reason is blank and the user count is the pack's bar. On a
    suppressed row the user count is an integer, the reason is the exact live text for one of the
    contract's source tables, and every registry column whose threshold bar exceeds the row's
    users is blank. Measures and derived labels may survive, so they aren't checked.
    """
    registry, shape = sources.registry, sources.shape
    header, rows = _read(fixture.text)
    findings = list(_trailing_order(fixture, header, shape))
    if not set(shape.trailing_order) <= set(header):
        return (
            *findings,
            Finding(
                "FIXTURE_SUPPRESSION_COLUMNS_MISSING",
                fixture.name,
                f"the header lacks {sorted(set(shape.trailing_order) - set(header))}.",
            ),
        )
    index = {name: position for position, name in enumerate(header)}
    fields = _fields_by_name(fixture.contract)
    bar = fixture.pack.expected_bar
    gated: list[tuple[str, int | None]] = []
    for name in header:
        field = fields.get(name)
        registered = None if field is None else _registered(fixture.contract, registry, field)
        if registered is not None:
            gated.append((name, shape.bars.get(registered.threshold)))

    tallies: dict[str, _Tally] = {}

    def note(code: str, row_number: int, text: str) -> None:
        tallies[code] = tallies.get(code, _Tally()).add(row_number, text)

    for row_number, row in enumerate(rows, start=1):
        flag = row[index[DISCRIMINATOR]]
        reason = row[index[REASON]]
        users_text = row[index[USER_COUNT]]
        if flag not in shape.discriminator_values:
            note("FIXTURE_DISCRIMINATOR_VALUE", row_number, f"{DISCRIMINATOR}={flag!r}")
            continue
        if flag != shape.suppressed_value:
            if reason:
                note("FIXTURE_UNSUPPRESSED_REASON", row_number, f"{REASON}={reason!r}")
            if users_text != str(bar):
                note("FIXTURE_UNSUPPRESSED_USER_COUNT", row_number, f"{USER_COUNT}={users_text!r}")
            continue
        users = int(users_text) if _is_integer(users_text) else None
        if users is None:
            note("FIXTURE_SUPPRESSED_USER_COUNT", row_number, f"{USER_COUNT}={users_text!r}")
            # A fixture-only reading of the observed form, so a trap's reason text and keys stay
            # checkable. Never a model for reading a real delivery's reason (amc.md §5).
            leading = reason.split(" ", 1)[0]
            users = int(leading) if _is_integer(leading) else None
        expected = set[str]() if users is None else _reasons(fixture.contract, shape, (users, bar))
        if reason not in expected:
            note("FIXTURE_SUPPRESSED_REASON", row_number, f"{REASON}={reason!r}")
        # With no readable user count, only a NONE column (bar 1) is let through: AMC nulls each
        # column against its own bar (docs/sources/amc.md §5), and no suppressed row observed live
        # named fewer than one user.
        floor = users if users is not None else 1
        kept = [
            name
            for name, column_bar in gated
            if row[index[name]] and (column_bar is None or column_bar > floor)
        ]
        if kept:
            note("FIXTURE_SUPPRESSED_KEY_PRESENT", row_number, f"kept {kept}")

    findings.extend(
        Finding(code, fixture.name, f"{tally.count} row(s), {tally.first}.")
        for code, tally in sorted(tallies.items())
    )
    return tuple(findings)


def _reasons(contract: DataContract, shape: SuppressionOutput, counts: tuple[int, int]) -> set[str]:
    """Every reason a suppressed row of this contract may carry for its user count and bar.

    A single-source contract's row names its table in the form observed on 2026-10-08. A pack
    reading several sources names the one path that fell short, quoted, with `(path i of n)`, the
    form the 2026-10-09 multi-source runs showed (`validation/RESULTS.md`); paths are numbered in
    `source_tables` order, which is the fixture's convention, not a verified AMC order.
    """
    users, bar = counts
    tables = contract.source_tables
    found = {shape.reason(users=users, table=table, bar=bar) for table in tables}
    if len(tables) > 1:
        found.update(
            PathReason(users, table, index, len(tables), bar).text
            for index, table in enumerate(tables, start=1)
        )
    return found


@dataclass(frozen=True, slots=True)
class _Column:
    """What one header column is checked as: its AMC type, a refusal, or a marker it used."""

    amc_type: str | None = None
    finding: Finding | None = None
    applied: ColumnMarker | None = None


class _GeneratedColumns:
    """Classify each header column of one generated fixture against its contract and registry."""

    # The discriminator's values are the suppression check's to judge, so it isn't typed twice.
    _OPTION_TYPES: Final = {REASON: "STRING", USER_COUNT: "INTEGER"}

    def __init__(
        self, fixture: FixtureUnderTest, sources: Sources, declaration: ConformanceDeclaration
    ) -> None:
        self.fixture = fixture
        self.sources = sources
        self.fields = _fields_by_name(fixture.contract)
        self.markers = {
            marker.column: marker
            for marker in declaration.markers
            if marker.contract_id == fixture.contract.contract_id
        }

    def _finding(self, code: str, detail: str) -> _Column:
        return _Column(finding=Finding(code, self.fixture.name, detail))

    def column(self, name: str) -> _Column:
        if name in WINDOW_COLUMNS:
            return _Column(amc_type="TIMESTAMP")
        if name in self.sources.shape.trailing_order:
            return _Column(amc_type=self._OPTION_TYPES.get(name))
        if name in self.fixture.contract.delivered_check_columns:
            # A currency's MIN twin: read by the load's currency check, never stored.
            return _Column(amc_type="STRING")
        field = self.fields.get(name)
        if field is None:
            return self._finding(
                "FIXTURE_COLUMN_UNMAPPED",
                f"{name} is not delivered by any field of {self.fixture.contract.contract_id}.",
            )
        marker = self.markers.pop(name, None)
        if field.resolves_against_registry:
            return self._registry_column(field, marker)
        return self._derived_column(field, marker)

    def _derived_column(self, field: FieldMapping, marker: ColumnMarker | None) -> _Column:
        name = field.delivered_name
        if marker is not None:
            return self._finding(
                "FIXTURE_MARKER_STALE",
                f"{name} is marked {marker.status} but isn't a registry column.",
            )
        declared = resolve_target_type(field, None)
        if declared is None:
            return self._finding(
                "FIXTURE_TYPE_UNKNOWN", f"{name} is derived and its contract declares no type."
            )
        return _Column(amc_type=_CANONICAL_AS_AMC[declared])

    def _registry_column(self, field: FieldMapping, marker: ColumnMarker | None) -> _Column:
        name = field.delivered_name
        registered = _registered(self.fixture.contract, self.sources.registry, field)
        if registered is None or registered.source_type is None:
            return self._finding(
                "FIXTURE_COLUMN_UNREGISTERED",
                f"{name} is not a registered column of "
                f"{', '.join(self.fixture.contract.source_tables)}.",
            )
        amc_type = registered.source_type.upper()
        pending = (registered.table, registered.name) in self.sources.pending
        status = "disputed" if registered.disputed else "pending" if pending else None
        if status is None and marker is not None:
            return self._finding(
                "FIXTURE_MARKER_STALE",
                f"{name} is marked {marker.status} but the registry records no "
                f"{marker.status} claim on {registered.table}.",
            )
        if status is not None and (marker is None or marker.status != status):
            return self._finding(
                f"FIXTURE_COLUMN_{status.upper()}_UNMARKED",
                f"{name} is {status} on {registered.table} and no marker allows it.",
            )
        return _Column(amc_type=amc_type, applied=marker)

    def unused_markers(self) -> tuple[Finding, ...]:
        return tuple(
            Finding(
                "FIXTURE_MARKER_STALE",
                self.fixture.name,
                f"{marker.column} is marked {marker.status} but the fixture carries no such "
                "column.",
            )
            for marker in self.markers.values()
        )


def _type_findings(
    fixture: FixtureUnderTest, types: Mapping[str, str], shape: SuppressionOutput
) -> tuple[Finding, ...]:
    """Refuse every column holding a value its AMC type can't parse, once per column."""
    header, rows = _read(fixture.text)
    parsers = _parsers(shape)
    findings: list[Finding] = []
    for position, name in enumerate(header):
        amc_type = types.get(name)
        if amc_type is None:
            continue
        parser = parsers.get(amc_type)
        if parser is None:
            findings.append(
                Finding(
                    "FIXTURE_TYPE_UNKNOWN",
                    fixture.name,
                    f"{name} has AMC type {amc_type}, which no parser covers.",
                )
            )
            continue
        tally = _Tally()
        for row_number, row in enumerate(rows, start=1):
            value = row[position] if position < len(row) else ""
            if value and not parser(value):
                tally = tally.add(row_number, repr(value))
        if tally.count:
            findings.append(
                Finding(
                    "FIXTURE_TYPE_MISMATCH",
                    fixture.name,
                    f"{name} is {amc_type}; {tally.count} row(s), {tally.first}.",
                )
            )
    return tuple(findings)


def _deliveries(text: str) -> tuple[str, ...]:
    """Split a periodic fixture into its deliveries: the rows sharing one AMC window each."""
    header, rows = _read(text)
    if not header or not set(WINDOW_COLUMNS) <= set(header):
        return (text,)
    start, end = (header.index(name) for name in WINDOW_COLUMNS)
    windows: dict[tuple[str, str], list[tuple[str, ...]]] = {}
    for row in rows:
        windows.setdefault((row[start], row[end]), []).append(row)
    return tuple(write_rows(header, grouped) for grouped in windows.values())


_REFUSED = "RESULT_SUPPRESSION_REFUSED"


def _delivery_day(text: str) -> date | None:
    """Return the one UTC day a daily delivery's window covers, or None when it isn't one day."""
    header, rows = _read(text)
    if not rows or not set(WINDOW_COLUMNS) <= set(header):
        return None
    start, end = (header.index(name) for name in WINDOW_COLUMNS)
    window = _window_days(rows[0][start], rows[0][end])
    return None if window is None or window[0] != window[1] else window[0]


def _result_findings(fixture: FixtureUnderTest) -> list[Finding]:
    """Run the producer's own result check per delivery; a periodic or daily file holds many.

    A daily distinct-count pack's declared refused day is the one delivery its
    `reject_delivery` policy must refuse (pack 4): that refusal is expected there, and its
    absence is a finding, as is a refusal on any other day.
    """
    split = fixture.pack.period_policy is not None or fixture.contract.contract_id in DAILY_BOUNDS
    texts = _deliveries(fixture.text) if split else (fixture.text,)
    findings: list[Finding] = []
    refused_days = fixture.refused_days
    refused: set[date] = set()
    for text in texts:
        summary, _evidence = parse_csv_result(text.encode())
        day = _delivery_day(text)
        for item in validate_result(fixture.pack, summary).findings:
            if item.code == _REFUSED and day is not None and day in refused_days:
                refused.add(day)
                continue
            finding = Finding(item.code, fixture.name, item.detail)
            if finding not in findings:
                findings.append(finding)
    findings.extend(
        Finding(
            "FIXTURE_REFUSAL_MISSING",
            fixture.name,
            f"the delivery for {day} is declared refused, and no suppressed row refuses it.",
        )
        for day in sorted(refused_days - refused)
    )
    return findings


_GRAINS: Final = {
    "week": TimeGrain.WEEK,
    "month": TimeGrain.MONTH,
    "quarter": TimeGrain.QUARTER,
    # A cohort vintage: the 13 calendar months ending with its as-of month (ADR-104).
    "custom": TimeGrain.CUSTOM,
}


def _window_days(start: str, end: str) -> tuple[date, date] | None:
    """Read a half-open AMC window into its inclusive days, or None when it isn't one."""
    try:
        first = datetime.fromisoformat(start.removesuffix("Z") + "+00:00")
        after = datetime.fromisoformat(end.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    midnight = datetime.min.time()
    if first.time() != midnight or after.time() != midnight or after <= first:
        return None
    return first.date(), date.fromordinal(after.date().toordinal() - 1)


def check_periodic(
    fixture: FixtureUnderTest, corpus: str | None, platform: str | None = None
) -> tuple[Finding, ...]:
    """Hold a periodic fixture to its declared grains and to the corpus that bounds its buyers.

    Every row's window must be exactly one complete declared period (ISO week Monday to Sunday,
    calendar month, UTC). And on every kept row each distinct-buyer count is at most the matching
    credited purchases in the attributed-conversion corpus over the same days and marketplace,
    and new-to-brand buyers never exceed attributed buyers: a buyer made at least one purchase.
    A finer grain set's row (pack 2, one per ad product) is bounded by its own ad product's
    purchases, and by the same period's platform row (`platform`, pack 1's fixture): the shoppers
    who bought after one ad product's ads are among those who bought after any. So a kept finer
    row whose period has no kept platform row is refused too, never left unbounded.
    """
    policy = fixture.pack.period_policy
    if policy is None or _escape_findings(fixture.name, fixture.text):
        return ()
    if corpus is not None and _escape_findings("corpus", corpus):
        corpus = None
    header, rows = _read(fixture.text)
    index = {name: position for position, name in enumerate(header)}
    grains = tuple(_GRAINS[grain.value] for grain in policy.grains)
    bound = BUYER_BOUNDS.get(fixture.contract.contract_id)
    totals = _corpus_totals(corpus, bound)
    ceilings = _platform_rows(platform, bound)
    tallies: dict[str, _Tally] = {}

    def note(code: str, row_number: int, text: str) -> None:
        tallies[code] = tallies.get(code, _Tally()).add(row_number, text)

    for row_number, row in enumerate(rows, start=1):
        window = _window_days(*(row[index[name]] for name in WINDOW_COLUMNS))
        if window is None or not any(is_complete_period(grain, *window) for grain in grains):
            rendered = ",".join(row[index[name]] for name in WINDOW_COLUMNS)
            note("FIXTURE_PERIOD_WINDOW_MISALIGNED", row_number, f"window {rendered}")
            continue
        if bound is None or row[index[DISCRIMINATOR]] != "false":
            continue
        marketplace = row[index[bound.marketplace_target]]
        split = "" if bound.split_column is None else row[index[bound.split_column]]
        counts = {target: int(row[index[target]] or 0) for target, _source in bound.pairs}
        exceeded: list[str] = []
        for target, source in bound.pairs:
            ceiling = sum(
                value
                for (day, market, column, value_of), value in totals.items()
                if column == source
                and market == marketplace
                and value_of == split
                and window[0] <= day <= window[1]
            )
            if counts[target] > ceiling:
                exceeded.append(f"{target}={counts[target]} > {source}={ceiling}")
        period_key = (row[index[WINDOW_COLUMNS[0]]], row[index[WINDOW_COLUMNS[1]]], marketplace)
        above = [
            f"{target}={counts[target]} > platform {target}={limit}"
            for target, limit in ceilings.get(period_key, {}).items()
            if counts[target] > limit
        ]
        if (
            platform is not None
            and bound.platform_contract is not None
            and (period_key not in ceilings)
        ):
            # A suppressed or absent platform row bounds nothing, and a kept finer row can't
            # exist under it: its shoppers are among the platform's, which fell under the bar.
            above.append("no kept platform row for its period and marketplace")
        if above:
            note("FIXTURE_BUYERS_EXCEED_PLATFORM", row_number, "; ".join(above))
        first, second = (target for target, _source in bound.pairs)
        if counts[first] > counts[second]:
            exceeded.append(f"{first}={counts[first]} > {second}={counts[second]}")
        if exceeded:
            note("FIXTURE_BUYERS_EXCEED_PURCHASES", row_number, "; ".join(exceeded))
    return tuple(
        Finding(code, fixture.name, f"{tally.count} row(s), {tally.first}.")
        for code, tally in sorted(tallies.items())
    )


_EXPOSURE_BOUNDED: Final = (
    ("shoppers_reached", "impressions"),
    ("search_shoppers", "shoppers_reached"),
    ("detail_page_view_shoppers", "shoppers_reached"),
    ("purchasers", "shoppers_reached"),
    ("ntb_purchasers", "purchasers"),
    ("purchasers", "purchases"),
)
"""Pack 5's counts each bounded by another on a kept row: a shopper saw at least one impression,
every outcome shopper was reached, and a purchaser made at least one purchase."""


def check_exposure_groups(fixture: FixtureUnderTest) -> tuple[Finding, ...]:
    """Hold pack 5's fixture to its count bounds and to its one declared currency refusal.

    The declared refused delivery (`refused_days`, by period start) must carry a currency whose
    MAX and MIN twins differ, which the load refuses; no other delivery may.
    """
    contract = fixture.contract
    if contract.contract_id != EXPOSURE_CONTRACT or _escape_findings(fixture.name, fixture.text):
        return ()
    header, rows = _read(fixture.text)
    index = {name: position for position, name in enumerate(header)}
    tallies: dict[str, _Tally] = {}

    def note(code: str, row_number: int, text: str) -> None:
        tallies[code] = tallies.get(code, _Tally()).add(row_number, text)

    disagreeing: set[date] = set()
    for row_number, row in enumerate(rows, start=1):
        window = _window_days(*(row[index[name]] for name in WINDOW_COLUMNS))
        for column, minimum in contract.currency_minimums:
            high, low = row[index[column]], row[index[minimum]]
            if high and low and high != low and window is not None:
                disagreeing.add(window[0])
        if row[index[DISCRIMINATOR]] != "false":
            continue
        broken = [
            f"{low}={row[index[low]]} > {high}={row[index[high]]}"
            for low, high in _EXPOSURE_BOUNDED
            if row[index[low]] and row[index[high]] and int(row[index[low]]) > int(row[index[high]])
        ]
        if broken:
            note("FIXTURE_EXPOSURE_COUNTS_INCONSISTENT", row_number, "; ".join(broken))
    findings = [
        Finding(code, fixture.name, f"{tally.count} row(s), {tally.first}.")
        for code, tally in sorted(tallies.items())
    ]
    findings.extend(
        Finding(
            "FIXTURE_REFUSAL_MISSING",
            fixture.name,
            f"the delivery from {day} is declared refused, and no currency disagreement "
            "refuses it.",
        )
        for day in sorted(fixture.refused_periods - disagreeing)
    )
    findings.extend(
        Finding(
            "FIXTURE_CURRENCY_DISAGREEMENT",
            fixture.name,
            f"the delivery from {day} carries a currency disagreement but isn't declared refused.",
        )
        for day in sorted(disagreeing - fixture.refused_periods)
    )
    return tuple(findings)


_COUNT_RANGES: Final = {"4_6": (4, 6), "7_9": (7, 9), "10_plus": (10, None)}


def _bucket_bounds(bucket: str) -> tuple[int, int | None] | None:
    """Return a bucket's impressions per shopper (exact for a single count), or None."""
    if bucket in _COUNT_RANGES:
        return _COUNT_RANGES[bucket]
    return (int(bucket), int(bucket)) if bucket.isdigit() else None


def check_exposure_frequency(fixture: FixtureUnderTest) -> tuple[Finding, ...]:
    """Hold packs 6 and 7's fixtures to their bucket bounds and their one declared NULL bucket.

    On a kept row a bucket's impressions lie within its shoppers times its count (exactly, for a
    single-count bucket), every purchaser is a shopper, and every new-to-brand purchaser a
    purchaser. The declared refused delivery (by period start) must carry a NULL bucket on a kept
    row, which the load refuses (`FREQUENCY_BUCKET_NULL`); no other delivery may.
    """
    if fixture.contract.contract_id not in FREQUENCY_CONTRACTS or _escape_findings(
        fixture.name, fixture.text
    ):
        return ()
    header, rows = _read(fixture.text)
    index = {name: position for position, name in enumerate(header)}
    tallies: dict[str, _Tally] = {}

    def note(code: str, row_number: int, text: str) -> None:
        tallies[code] = tallies.get(code, _Tally()).add(row_number, text)

    nulls: set[date] = set()
    for row_number, row in enumerate(rows, start=1):
        if row[index[DISCRIMINATOR]] != "false":
            continue
        bucket = row[index["frequency_bucket"]]
        if not bucket:
            window = _window_days(*(row[index[name]] for name in WINDOW_COLUMNS))
            if window is not None:
                nulls.add(window[0])
            continue
        bounds = _bucket_bounds(bucket)
        shoppers, impressions = int(row[index["shoppers"]]), int(row[index["impressions"]])
        purchasers, ntb = int(row[index["purchasers"]]), int(row[index["ntb_purchasers"]])
        broken: list[str] = []
        if bounds is None:
            broken.append(f"frequency_bucket={bucket!r} is no declared bucket")
        else:
            low, high = bounds
            if impressions < low * shoppers or (high is not None and impressions > high * shoppers):
                broken.append(f"impressions={impressions} outside {bucket} x shoppers={shoppers}")
        if purchasers > shoppers:
            broken.append(f"purchasers={purchasers} > shoppers={shoppers}")
        if ntb > purchasers:
            broken.append(f"ntb_purchasers={ntb} > purchasers={purchasers}")
        if broken:
            note("FIXTURE_FREQUENCY_COUNTS_INCONSISTENT", row_number, "; ".join(broken))
    findings = [
        Finding(code, fixture.name, f"{tally.count} row(s), {tally.first}.")
        for code, tally in sorted(tallies.items())
    ]
    findings.extend(
        Finding(
            "FIXTURE_REFUSAL_MISSING",
            fixture.name,
            f"the delivery from {day} is declared refused, and no NULL bucket refuses it.",
        )
        for day in sorted(fixture.refused_periods - nulls)
    )
    findings.extend(
        Finding(
            "FIXTURE_FREQUENCY_BUCKET_NULL",
            fixture.name,
            f"the delivery from {day} carries a NULL bucket but isn't declared refused.",
        )
        for day in sorted(nulls - fixture.refused_periods)
    )
    return tuple(findings)


_COHORT_SIZES = ("cohort_buyers_new_to_brand_12mo", "cohort_buyers_account_ids")
_COHORT_CHECK = "cohort_buyers_account_ids_check"


def check_acquisition_cohorts(fixture: FixtureUnderTest) -> tuple[Finding, ...]:
    """Hold packs 14 and 15's fixtures to their cohort bounds and their one declared refusal.

    On a kept row repeat buyers, first-time repeaters and cumulative repeaters never exceed the
    cohort's size (pack 15: its own account-ID count), and first-time never exceed cumulative.
    Each vintage repeats one size per cohort and source set on every follow-up row, except the
    declared refused vintage (by period start), which must repeat one differently: the load
    refuses it (`REPEATED_TOTAL_MISMATCH`); no other vintage may.
    """
    if fixture.contract.contract_id not in COHORT_CONTRACTS or _escape_findings(
        fixture.name, fixture.text
    ):
        return ()
    header, rows = _read(fixture.text)
    index = {name: position for position, name in enumerate(header)}
    standard = fixture.contract.contract_id == COHORT_STANDARD
    sizes = _COHORT_SIZES if standard else (_COHORT_CHECK,)
    tallies: dict[str, _Tally] = {}
    copies: dict[tuple[str, str, str, str], set[str]] = {}
    starts: dict[str, date] = {}
    for row_number, row in enumerate(rows, start=1):
        if row[index[DISCRIMINATOR]] != "false":
            continue
        window = _window_days(*(row[index[name]] for name in WINDOW_COLUMNS))
        vintage = row[index[WINDOW_COLUMNS[0]]]
        if window is not None:
            starts[vintage] = window[0]
        key = (vintage, row[index["cohort_month"]], row[index["acquisition_source_set"]])
        for column in sizes:
            copies.setdefault((*key, column), set()).add(row[index[column]])
        size = int(row[index[sizes[0]]])
        repeat = int(row[index["repeat_buyers"]])
        first = int(row[index["first_repeat_buyers"]])
        cumulative = int(row[index["cumulative_repeat_buyers"]])
        if repeat > size or cumulative > size or first > cumulative:
            tallies["FIXTURE_COHORT_COUNTS_INCONSISTENT"] = tallies.get(
                "FIXTURE_COHORT_COUNTS_INCONSISTENT", _Tally()
            ).add(
                row_number,
                f"size={size} repeat={repeat} first={first} cumulative={cumulative}",
            )
    mismatched = {
        starts[key[0]] for key, values in copies.items() if len(values) > 1 and key[0] in starts
    }
    findings = [
        Finding(code, fixture.name, f"{tally.count} row(s), {tally.first}.")
        for code, tally in sorted(tallies.items())
    ]
    findings.extend(
        Finding(
            "FIXTURE_REFUSAL_MISSING",
            fixture.name,
            f"the vintage from {day} is declared refused, and no repeated cohort size differs.",
        )
        for day in sorted(fixture.refused_periods - mismatched)
    )
    findings.extend(
        Finding(
            "FIXTURE_REPEATED_TOTAL_MISMATCH",
            fixture.name,
            f"the vintage from {day} repeats a cohort size differently but isn't declared refused.",
        )
        for day in sorted(mismatched - fixture.refused_periods)
    )
    return tuple(findings)


def check_daily(fixture: FixtureUnderTest, corpus: str | None) -> tuple[Finding, ...]:
    """Hold a daily distinct-count fixture to one-day windows and to the corpus that bounds it.

    Every row's window must be exactly one UTC day, its own row's day where the row kept it.
    On every kept row each distinct count is at most the same day's, campaign's and
    marketplace's corpus measure: a shopper reached was shown at least one impression.
    """
    bound = DAILY_BOUNDS.get(fixture.contract.contract_id)
    if bound is None or _escape_findings(fixture.name, fixture.text):
        return ()
    if corpus is not None and _escape_findings("corpus", corpus):
        corpus = None
    header, rows = _read(fixture.text)
    index = {name: position for position, name in enumerate(header)}
    ceilings = _daily_totals(corpus, bound)
    tallies: dict[str, _Tally] = {}

    def note(code: str, row_number: int, text: str) -> None:
        tallies[code] = tallies.get(code, _Tally()).add(row_number, text)

    for row_number, row in enumerate(rows, start=1):
        window = _window_days(*(row[index[name]] for name in WINDOW_COLUMNS))
        day = row[index[bound.date_column]]
        if window is None or window[0] != window[1] or (day and window[0].isoformat() != day):
            rendered = ",".join(row[index[name]] for name in WINDOW_COLUMNS)
            note("FIXTURE_DAY_WINDOW_MISALIGNED", row_number, f"window {rendered}")
            continue
        if row[index[DISCRIMINATOR]] != "false":
            continue
        key = (day, row[index[bound.campaign_column]], row[index[bound.marketplace_column]])
        exceeded = [
            f"{target}={row[index[target]]} > {source}={ceilings.get((*key, source), 0)}"
            for target, source in bound.pairs
            if int(row[index[target]] or 0) > ceilings.get((*key, source), 0)
        ]
        if exceeded:
            note("FIXTURE_DISTINCT_EXCEEDS_CORPUS", row_number, "; ".join(exceeded))
    return tuple(
        Finding(code, fixture.name, f"{tally.count} row(s), {tally.first}.")
        for code, tally in sorted(tallies.items())
    )


def _daily_totals(corpus: str | None, bound: DailyBound) -> dict[tuple[str, str, str, str], int]:
    """Sum the bounding corpus's measure per day, campaign, marketplace and column."""
    if corpus is None:
        return {}
    totals: dict[tuple[str, str, str, str], int] = {}
    for row in cast("tuple[dict[str, str], ...]", dict_rows(corpus)[1]):
        for _target, source in bound.pairs:
            key = (
                row[bound.date_column],
                row[bound.campaign_column],
                row[bound.marketplace_column],
                source,
            )
            totals[key] = totals.get(key, 0) + int(row[source] or 0)
    return totals


def _corpus_totals(
    corpus: str | None, bound: BuyerBound | None
) -> dict[tuple[date, str, str, str], int]:
    """Sum the bounding corpus's purchases per day, marketplace, column and split value.

    The split value is "" for a pack that doesn't split, so its rows sum the whole marketplace.
    """
    if corpus is None or bound is None:
        return {}
    totals: dict[tuple[date, str, str, str], int] = {}
    for row in cast("tuple[dict[str, str], ...]", dict_rows(corpus)[1]):
        day = date.fromisoformat(row[bound.date_column])
        split = "" if bound.split_column is None else row[bound.split_column]
        for _target, source in bound.pairs:
            key = (day, row[bound.marketplace_column], source, split)
            totals[key] = totals.get(key, 0) + int(row[source] or 0)
    return totals


def _platform_rows(
    platform: str | None, bound: BuyerBound | None
) -> dict[tuple[str, str, str], dict[str, int]]:
    """Read the coarser grain set's kept rows by window and marketplace, its counts the ceiling."""
    if platform is None or bound is None or bound.platform_contract is None:
        return {}
    rows: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in cast("tuple[dict[str, str], ...]", dict_rows(platform)[1]):
        if row[DISCRIMINATOR] != "false":
            continue
        key = (row[WINDOW_COLUMNS[0]], row[WINDOW_COLUMNS[1]], row[bound.marketplace_target])
        rows[key] = {target: int(row[target] or 0) for target, _source in bound.pairs}
    return rows


def check_generated(
    fixture: FixtureUnderTest, sources: Sources, declaration: ConformanceDeclaration
) -> GeneratedReport:
    """Hold one generated fixture to its pack, the registry, the markers and the live shape."""
    refused = _escape_findings(fixture.name, fixture.text)
    if refused:
        return GeneratedReport(findings=refused, markers=())
    findings = _result_findings(fixture)
    header, _rows = _read(fixture.text)
    classifier = _GeneratedColumns(fixture, sources, declaration)
    types: dict[str, str] = {}
    applied: list[ColumnMarker] = []
    for name in header:
        verdict = classifier.column(name)
        if verdict.finding is not None:
            findings.append(verdict.finding)
        if verdict.amc_type is not None:
            types[name] = verdict.amc_type
        if verdict.applied is not None:
            applied.append(verdict.applied)
    findings.extend(classifier.unused_markers())
    findings.extend(_type_findings(fixture, types, sources.shape))
    findings.extend(check_suppression(fixture, sources))
    return GeneratedReport(findings=tuple(findings), markers=tuple(applied))


def result_shape(pack: GovernedQueryPack, shape: SuppressionOutput) -> ResultShape:
    """Give the generator the layout of one pack's result: its columns, bar and live trailer."""
    return ResultShape(
        columns=pack.expected_columns,
        expected_bar=pack.expected_bar,
        trailing_order=shape.trailing_order,
        timestamp_format=shape.timestamp_format,
    )


@dataclass(frozen=True, slots=True)
class ConformanceRun:
    """Everything one run found: refusals, the markers it applied, the trap deviations it read."""

    findings: tuple[Finding, ...]
    markers: tuple[ColumnMarker, ...]
    trap_deviations: tuple[TrapDeviation, ...]


@dataclass(frozen=True, slots=True)
class ConformanceInputs:
    """The verified sources and fixtures one run reads; each a value so a test can swap one."""

    registry: ColumnRegistry
    contracts: Mapping[str, DataContract]
    packs: Mapping[str, GovernedQueryPack]
    shape: SuppressionOutput
    pending: frozenset[tuple[str, str]]
    declaration: ConformanceDeclaration
    generated: Mapping[str, str]
    """Generated AMC result fixtures, name to CSV text, keyed by contract id via `<id>.csv`."""

    traps: Mapping[str, str]
    """Hand-built AMC trap fixtures, `traps/<file>.csv` to CSV text."""

    refused_days: Mapping[str, frozenset[date]] = field(default_factory=dict[str, frozenset[date]])
    """Per daily-delivery contract, the days the synthetic catalog declares refused."""
    refused_periods: Mapping[str, frozenset[date]] = field(
        default_factory=dict[str, frozenset[date]]
    )
    """Per periodic contract, the deliveries the catalog declares the load refuses."""


TRAPS_DIR = Path("tests/fixtures/traps")
AMC_TRAP_GLOB = "amc-*.csv"


def inputs_from(root: Path, generated: Mapping[str, str]) -> ConformanceInputs:
    """Read every verified source and committed AMC trap from the repository."""
    shape = load_synthetic_entity_catalog(root).shape
    return ConformanceInputs(
        registry=load_registry(root),
        contracts={contract.contract_id: contract for _, contract in load_contracts(root)},
        packs={pack.contract_id: pack for pack in load_query_packs(root)},
        shape=load_suppression_output(root),
        pending=load_pending_columns(root),
        declaration=load_conformance_declaration(root),
        generated=generated,
        traps={
            f"traps/{path.name}": path.read_text()
            for path in sorted((root / TRAPS_DIR).glob(AMC_TRAP_GLOB))
        },
        refused_days={"amc-dsp-reach": frozenset({shape.dsp_reach.suppressed_day})},
        refused_periods={
            EXPOSURE_CONTRACT: frozenset({shape.exposure_groups.currency_disagreement_week}),
            **dict.fromkeys(
                FREQUENCY_CONTRACTS, frozenset({shape.exposure_frequency.null_bucket_month})
            ),
            # Pack 14's refused vintage, by its first day (REPEATED_TOTAL_MISMATCH).
            COHORT_STANDARD: frozenset(
                {vintage_ending(shape.acquisition_cohorts.refused_vintage).start}
            ),
        },
    )


def run_conformance(inputs: ConformanceInputs) -> ConformanceRun:
    """Check every generated AMC fixture and every AMC trap; report every finding at once."""
    sources = Sources(inputs.registry, inputs.shape, inputs.pending)
    findings: list[Finding] = []
    markers: list[ColumnMarker] = []
    checked: set[str] = set()
    for name, text in sorted(inputs.generated.items()):
        contract_id = Path(name).stem
        contract = inputs.contracts.get(contract_id)
        pack = inputs.packs.get(contract_id)
        if contract is None or pack is None:
            findings.append(
                Finding("FIXTURE_PACK_MISSING", name, f"no contract and pack {contract_id!r}.")
            )
            continue
        checked.add(contract_id)
        under_test = FixtureUnderTest(
            name,
            text,
            contract,
            pack,
            inputs.refused_days.get(contract_id, frozenset()),
            inputs.refused_periods.get(contract_id, frozenset()),
        )
        report = check_generated(under_test, sources, inputs.declaration)
        findings.extend(report.findings)
        markers.extend(report.markers)
        bound = BUYER_BOUNDS.get(contract_id)
        findings.extend(
            check_periodic(
                under_test,
                None if bound is None else inputs.generated.get(f"{bound.corpus_contract}.csv"),
                None
                if bound is None or bound.platform_contract is None
                else inputs.generated.get(f"{bound.platform_contract}.csv"),
            )
        )
        findings.extend(check_exposure_groups(under_test))
        findings.extend(check_exposure_frequency(under_test))
        findings.extend(check_acquisition_cohorts(under_test))
        daily = DAILY_BOUNDS.get(contract_id)
        findings.extend(
            check_daily(
                under_test,
                None if daily is None else inputs.generated.get(f"{daily.corpus_contract}.csv"),
            )
        )
    findings.extend(
        Finding(
            "FIXTURE_MARKER_STALE",
            marker.contract_id,
            f"{marker.column} is marked {marker.status} but no generated fixture is checked "
            "for that contract.",
        )
        for marker in inputs.declaration.markers
        if marker.contract_id not in checked
    )
    deviations = {item.fixture: item for item in inputs.declaration.trap_deviations}
    for name, text in sorted(inputs.traps.items()):
        deviation = deviations.pop(name, None)
        contract_id = deviation.contract_id if deviation is not None else ""
        contract = inputs.contracts.get(contract_id)
        pack = inputs.packs.get(contract_id)
        if deviation is not None and (contract is None or pack is None):
            findings.append(
                Finding("FIXTURE_PACK_MISSING", name, f"no contract and pack {contract_id!r}.")
            )
            continue
        if deviation is None or contract is None or pack is None:
            findings.append(
                Finding(
                    "TRAP_UNDECLARED",
                    name,
                    "an AMC trap needs a trap_deviations entry, even one with no codes.",
                )
            )
            continue
        findings.extend(
            check_trap(FixtureUnderTest(name, text, contract, pack), sources, deviation)
        )
    findings.extend(
        Finding("TRAP_DECLARATION_ORPHANED", fixture, "no AMC trap of that name was read.")
        for fixture in sorted(deviations)
    )
    return ConformanceRun(
        findings=tuple(findings),
        markers=tuple(markers),
        trap_deviations=inputs.declaration.trap_deviations,
    )


def check_trap(
    fixture: FixtureUnderTest, sources: Sources, deviation: TrapDeviation
) -> tuple[Finding, ...]:
    """Refuse a trap whose suppression differences aren't exactly the ones it declares."""
    refused = _escape_findings(fixture.name, fixture.text)
    if refused:
        return refused
    actual = tuple(sorted({item.code for item in check_suppression(fixture, sources)}))
    if actual == deviation.codes:
        return ()
    return (
        Finding(
            "TRAP_DEVIATION_DRIFT",
            fixture.name,
            f"declared {list(deviation.codes)}, found {list(actual)}.",
        ),
    )
