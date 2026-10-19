# SPDX-License-Identifier: MIT-0
"""Rules a data contract must satisfy, checked before any data exists.

Every rule here corresponds to a prohibition in the data-contract-definition or
data-contract-amc steering, or to a fact recorded in contracts/shared/amc-columns.yaml.
The steering's own authoring test says that if a script can enforce a rule, the script
should exist and the rule should be deleted — this module is that script's brain, and
several steering rules become deletable once it runs in `make validate-contracts`.

Findings rather than exceptions, deliberately. A contract with four problems should
report four, because discovering the next one on the following run turns one correction
into an afternoon of them. Structural impossibilities still raise at construction in
`contract.py`; everything requiring a cross-reference lives here.

Pure domain. The AMC column registry arrives as a `ColumnRegistry` built by an adapter
from contracts/shared/amc-columns.yaml, so every rule is testable without a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations
from typing import TYPE_CHECKING

from domain.data_contracts.contract import DataContract, NullMeaning
from domain.data_contracts.schema import (
    CONFORMED_PROVENANCE_COLUMNS,
    ESCALATING_THRESHOLDS,
    NON_OUTPUTTABLE_THRESHOLDS,
    ColumnRegistry,
    RegisteredColumn,
    resolve_target_type,
)

if TYPE_CHECKING:  # pragma: no cover — type-only
    from collections.abc import Sequence

__all__ = [
    "ESCALATING_THRESHOLDS",
    "NON_OUTPUTTABLE_THRESHOLDS",
    "ColumnRegistry",
    "Finding",
    "RegisteredColumn",
    "Severity",
    "check_delivery_diagnostics_are_not_conformed",
    "check_shared_measures_are_separable",
    "has_errors",
    "validate",
]

# Ratios are derived in the semantic layer from additive facts, never stored: they are
# not summable, so a stored ratio is wrong the moment anything aggregates it.
RATIO_TOKENS = frozenset({"roas", "ctr", "cvr", "cpc", "cpm", "acos", "tacos"})

# It takes two source currencies collapsing onto one column before agreement is a claim
# worth asserting. With one there is nothing to disagree with.
_CURRENCIES_THAT_CAN_DISAGREE = 2

# Encoding grain in a table name makes every regrain a rename.
GRAIN_TOKENS_IN_NAME = (
    "_daily",
    "_hourly",
    "_weekly",
    "_monthly",
    "_by_day",
    "_by_hour",
    "_by_week",
    "_by_month",
)


SEVERAL_WRITERS = 2
"""The point at which a fact's contracts can collide with each other.

One writer owns its table outright; two is where separability starts to matter.
"""

_CONFORMED_TAXONOMY_ATTRIBUTES = frozenset({"channel_group_key"})
"""Functionally dependent governed axes that are context, not contributed measures."""

CANONICAL_TAXONOMY_COLUMNS = frozenset(
    {
        "ecosystem_key",
        "platform_key",
        "channel_group_key",
        "channel_key",
        "ad_product_key",
        "exposure_group",
        "acquisition_channel_group",
    }
)
"""Every canonical axis contracts/shared/media-taxonomy.yaml resolves. No pack outputs one."""

EXISTING_DELIVERED_TAXONOMY_KEYS = frozenset(
    {
        ("amc-attributed-conversion", 1, "channel_group_key"),
        ("amc-deduplicated-temporal-attribution", 1, "channel_group_key"),
        ("amc-reach-frequency", 1, "channel_group_key"),
    }
)
"""Packs that already derive `channel_group_key` with a SQL `CASE`, named rather than hidden.

Business-outcomes design §4.2 records the pattern as an existing deviation, not changed by that
design, and refers it to the owner. Changing these packs is a new `packVersion` and a live
re-extraction, so it's a decision, not a side effect of adding a gate. Keyed on the contract
version, so a new version must drop the derivation rather than inherit the exception.
"""

ATTRIBUTED_EVENT_TABLES = frozenset(
    {
        "amazon_attributed_events_by_conversion_time",
        "amazon_attributed_events_by_traffic_time",
    }
)
"""The two tables that pair each conversion with one credited traffic event, without an ID."""

CAMPAIGN_SOURCE_COLUMN = "campaign_id_string"

SOURCE_RATIO_TOKENS = RATIO_TOKENS | frozenset(
    {"pct", "percent", "percentage", "share", "ratio", "rate", "fraction", "proportion"}
)
"""Words that mark a source field as a ratio or share the source computed (AC-BR.10)."""

_NAME_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class Severity(StrEnum):
    """Whether a finding blocks the gate."""

    ERROR = "error"
    """Fails `make validate-contracts`. The contract does not load."""

    WARNING = "warning"
    """Reported and does not block. Used where the rule cannot be certain."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem, traceable to the rule that found it."""

    rule: str
    severity: Severity
    contract_id: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.contract_id}: {self.rule} — {self.message}"


def _lookup_in(
    contract: DataContract, registry: ColumnRegistry, column: str
) -> RegisteredColumn | None:
    """Resolve a column against the contract's source tables, first match winning."""
    return next(
        (
            candidate
            for table in contract.source_tables
            if (candidate := registry.lookup(table=table, column=column)) is not None
        ),
        None,
    )


def _derived_input_columns(
    contract: DataContract, registry: ColumnRegistry, entry: str
) -> tuple[tuple[str, RegisteredColumn], ...]:
    """Resolve one `derived_from` entry to every (source table, column) it may read.

    An entry is a bare column, resolved against each source table that has it in declared
    order, or `table.column`, resolved against that one source table only. The qualified form
    exists for a contract over several tables that share a column name — `conversions` and the
    attributed-events tables both carry `total_product_sales` — where a rule must know which
    table a measure is summed from.
    """
    table, separator, column = entry.rpartition(".")
    if separator:
        if table not in contract.source_tables:
            return ()
        found = registry.lookup(table=table, column=column)
        return () if found is None else ((table, found),)
    return tuple(
        (source_table, found)
        for source_table in contract.source_tables
        if (found := registry.lookup(table=source_table, column=entry)) is not None
    )


def _source_column(source_path: str) -> str:
    """Extract the bare column name from a source path.

    AMC paths are flat column names; vendor paths are nested like
    `salesByAsin[].shippedRevenue.amount`. Taking the last segment is enough to resolve
    an AMC column and harmless for vendor, where the registry does not apply.
    """
    return source_path.rsplit(".", maxsplit=1)[-1].replace("[]", "")


def check_money_is_never_bare(contract: DataContract) -> list[Finding]:
    """Require a divisor and a currency column on every monetary column.

    A bare amount is unusable: no source provides FX, so an amount without its currency
    cannot be converted later and cannot be compared across marketplaces.
    """
    findings: list[Finding] = []
    declared = set(contract.target_columns)
    for mapping in contract.monetary_fields:
        if not mapping.currency_column:
            findings.append(
                Finding(
                    rule="money-carries-currency",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{mapping.target_column} is monetary and declares no "
                        "currency_column. Never store a bare monetary amount."
                    ),
                )
            )
        elif mapping.currency_column not in declared:
            findings.append(
                Finding(
                    rule="money-carries-currency",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{mapping.target_column} names currency_column "
                        f"{mapping.currency_column!r}, which this contract does not map. "
                        "The currency must travel with the amount."
                    ),
                )
            )
    return findings


def _sibling_currency_path(source_path: str) -> str | None:
    """Derive the currency path sitting beside a nested money amount.

    Vendor money is `{amount, currencyCode}`, so `shippedCogs.amount` implies
    `shippedCogs.currencyCode`. Returns None for a scalar path, which is every AMC
    column — AMC carries one currency per table rather than one per value.
    """
    suffix = ".amount"
    if not source_path.endswith(suffix):
        return None
    return f"{source_path[: -len(suffix)]}.currencyCode"


def check_currency_agreement_is_declared(contract: DataContract) -> list[Finding]:
    """Require the agreement assertion wherever amounts share one currency column.

    One `currency_code` per fact keeps the model uniform, but on a source that carries
    currency per value it stores less than we were given (ADR-021). Two of three vendor
    currency values are discarded, so nothing downstream could ever detect that they
    disagreed — the same shape of problem as AMC suppression without the discriminator.

    The expected set is derived from the contract's own field mappings rather than read
    from the declaration, so a shorter list cannot satisfy the rule.
    """
    by_currency_column: dict[str, list[str]] = {}
    for mapping in contract.monetary_fields:
        sibling = _sibling_currency_path(mapping.source_path)
        if mapping.currency_column and sibling:
            by_currency_column.setdefault(mapping.currency_column, []).append(sibling)

    declared = set(contract.currency_agreement)
    findings: list[Finding] = []
    for currency_column, siblings in sorted(by_currency_column.items()):
        distinct = set(siblings)
        if len(distinct) < _CURRENCIES_THAT_CAN_DISAGREE:
            continue
        missing = sorted(distinct - declared)
        if missing:
            findings.append(
                Finding(
                    rule="currency-agreement-declared",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{len(distinct)} source currencies collapse onto "
                        f"{currency_column!r}, and currency_agreement omits "
                        f"{', '.join(missing)}. Every one must be asserted equal at load "
                        "time, or a mixed-currency row silently takes whichever amount "
                        "was mapped first."
                    ),
                )
            )
    return findings


def check_no_stored_ratios(contract: DataContract) -> list[Finding]:
    """No ratio is ever stored; ratios are derived in the semantic layer.

    A stored ROAS is wrong as soon as two rows are added together, and it hides which
    numerator and window produced it — which ADR-017 exists to pin down.
    """
    findings: list[Finding] = []
    for column in contract.target_columns:
        tokens = set(column.lower().split("_"))
        offending = tokens & RATIO_TOKENS
        if offending:
            findings.append(
                Finding(
                    rule="no-stored-ratios",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column} looks like a stored ratio ({', '.join(sorted(offending))}). "
                        "Ratios are declared in contracts/shared/metric-dictionary.yaml and "
                        "computed on read, because they are not summable."
                    ),
                )
            )
    return findings


def check_grain_not_in_table_name(contract: DataContract) -> list[Finding]:
    """Grain is declared, never encoded in a name, or a regrain becomes a rename."""
    lowered = contract.target_table.lower()
    for token in GRAIN_TOKENS_IN_NAME:
        if token in lowered:
            return [
                Finding(
                    rule="grain-not-in-table-name",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"target_table {contract.target_table!r} encodes its grain "
                        f"({token!r}). Declare grain in `grain`; a regrain must not be a "
                        "table rename."
                    ),
                )
            ]
    return []


def check_grain_columns_are_mapped(contract: DataContract) -> list[Finding]:
    """Every grain and partition column is actually produced by this contract."""
    findings: list[Finding] = []
    declared = set(contract.target_columns)
    suppression_columns: set[str] = (
        set(contract.suppression.all_columns) if contract.suppression else set()
    )
    for label, columns in (("grain", contract.grain), ("partition_keys", contract.partition_keys)):
        for column in columns:
            if column not in declared and column not in suppression_columns:
                findings.append(
                    Finding(
                        rule="keys-are-mapped",
                        severity=Severity.ERROR,
                        contract_id=contract.contract_id,
                        message=(f"{label} names {column!r}, which no field mapping produces."),
                    )
                )
    return findings


def check_amc_columns_are_verified(
    contract: DataContract, registry: ColumnRegistry
) -> list[Finding]:
    """Every AMC source column is in the registry, outputtable, and not disputed.

    The registry is an allowlist. An absent column is unverified rather than absent from
    AMC, and using one means a contract asserts a fact no source document supports —
    which the steering forbids outright.
    """
    if not contract.is_amc:
        return []

    findings: list[Finding] = []
    for table in contract.source_tables:
        if table not in registry.known_tables:
            findings.append(
                Finding(
                    rule="amc-table-transcribed",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"source table {table!r} is not transcribed in "
                        "contracts/shared/amc-columns.yaml. Transcribe its datasources "
                        "page before a contract depends on it."
                    ),
                )
            )

    for mapping in contract.fields:
        if not mapping.resolves_against_registry:
            continue
        column = _source_column(mapping.source_path)
        found = _lookup_in(contract, registry, column)
        if found is None:
            findings.append(
                Finding(
                    rule="amc-column-verified",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} is not in the registry for any of "
                        f"{', '.join(contract.source_tables)}. An unverified column may "
                        "not be used; add it with a citation first."
                    ),
                )
            )
            continue

        if found.disputed:
            findings.append(
                Finding(
                    rule="amc-column-disputed",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} on {found.table} is marked `status: disputed`: our "
                        "source doc and the live page disagree. Settle it with a probe "
                        "before depending on it."
                    ),
                )
            )
        if not found.outputtable:
            findings.append(
                Finding(
                    rule="amc-column-outputtable",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} is threshold {found.threshold} and can never appear "
                        "in a final SELECT. It is usable only inside a CTE."
                    ),
                )
            )
        if found.deprecated_for_use and found.preferred_alternative:
            findings.append(
                Finding(
                    rule="amc-preferred-column",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} should be {found.preferred_alternative!r}. The wrong "
                        "one drops rows silently instead of erroring."
                    ),
                )
            )
    return findings


def check_derived_inputs_are_usable(
    contract: DataContract, registry: ColumnRegistry
) -> list[Finding]:
    """Verify the columns a derived value reads, without applying the output rules to them.

    A derived input is exempt from `outputtable` on purpose: reach reads `user_id`, which is
    `VERY_HIGH` and legal only inside a CTE, and that exemption is the only reason exact
    reach is obtainable. `INTERNAL` gets no such exemption — it is not usable anywhere, so
    it cannot rescue a value by hiding in a subquery.
    """
    if not contract.is_amc:
        return []

    findings: list[Finding] = []
    for mapping in contract.fields:
        for input_column in mapping.derived_from:
            found = next(
                (column for _, column in _derived_input_columns(contract, registry, input_column)),
                None,
            )
            if found is None:
                findings.append(
                    Finding(
                        rule="amc-column-verified",
                        severity=Severity.ERROR,
                        contract_id=contract.contract_id,
                        message=(
                            f"{mapping.target_column} derives from {input_column!r}, which is "
                            f"not in the registry for any of "
                            f"{', '.join(contract.source_tables)}."
                        ),
                    )
                )
            elif found.threshold == "INTERNAL":
                findings.append(
                    Finding(
                        rule="amc-derived-input-usable",
                        severity=Severity.ERROR,
                        contract_id=contract.contract_id,
                        message=(
                            f"{mapping.target_column} derives from {input_column!r}, which is "
                            "INTERNAL. VERY_HIGH is legal inside a CTE; INTERNAL is not "
                            "usable at all."
                        ),
                    )
                )
    return findings


def check_divisors_match_registry(
    contract: DataContract, registry: ColumnRegistry
) -> list[Finding]:
    """Compare each declared scale divisor against the one the registry records.

    The highest-value rule in this module. A wrong divisor is a silent 1000x error on a
    fee, or a 5-to-8 order-of-magnitude error in the numerator of every ROAS — and both
    produce numbers that look entirely plausible.
    """
    if not contract.is_amc:
        return []

    findings: list[Finding] = []
    for mapping in contract.fields:
        if not mapping.resolves_against_registry:
            continue
        column = _source_column(mapping.source_path)
        found = _lookup_in(contract, registry, column)
        if found is None:
            continue

        if found.monetary and not mapping.monetary:
            findings.append(
                Finding(
                    rule="divisor-declared",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} is monetary in the registry (divisor "
                        f"{found.scale_divisor}) and this contract declares no "
                        "scale_divisor for it."
                    ),
                )
            )
        elif (
            found.monetary
            and found.scale_divisor is not None
            and mapping.scale_divisor != found.scale_divisor
        ):
            findings.append(
                Finding(
                    rule="divisor-matches-registry",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} declares divisor {mapping.scale_divisor} but the "
                        f"registry records {found.scale_divisor}. The divisor is a "
                        "property of the column, never the table."
                    ),
                )
            )
        elif not found.monetary and mapping.monetary:
            findings.append(
                Finding(
                    rule="divisor-matches-registry",
                    severity=Severity.WARNING,
                    contract_id=contract.contract_id,
                    message=(
                        f"{column!r} declares divisor {mapping.scale_divisor} but the "
                        "registry does not record it as monetary. Verify before trusting."
                    ),
                )
            )
    return findings


def check_suppression_is_declared(
    contract: DataContract, registry: ColumnRegistry
) -> list[Finding]:
    """If any output column escalates the threshold, suppression must be visible.

    Without the discriminator, AMC may null identifying values without a flag, so privacy
    withholding is indistinguishable from an ordinary null. The evidence is lost inside AMC
    before delivery; this is the one rule whose violation cannot be repaired downstream.
    violation can never be detected downstream. That is why it is checked here, before
    any data exists.
    """
    if not contract.is_amc:
        return []

    # Derived inputs are excluded: the bar is set by the columns in the final output, and a
    # CTE column is not one of them. That is the only reason reach is obtainable at all.
    escalating = [
        column
        for mapping in contract.fields
        if mapping.resolves_against_registry
        and (column := _source_column(mapping.source_path))
        and (found := _lookup_in(contract, registry, column)) is not None
        and found.escalates
    ]
    if not escalating or contract.suppression is not None:
        return []

    return [
        Finding(
            rule="suppression-declared",
            severity=Severity.ERROR,
            contract_id=contract.contract_id,
            message=(
                f"output includes {', '.join(sorted(set(escalating)))}, which need 100 "
                "distinct users per row, and no suppression columns are declared. "
                "AMC's default nulls privacy-filtered values without identifying them."
            ),
        )
    ]


def check_suppression_declaration_is_consistent(contract: DataContract) -> list[Finding]:
    """Check a declared suppression block against the rest of the contract.

    Split from the rule above because the two fire on different conditions. That rule asks
    whether suppression *must* be declared, which depends on threshold escalation. These
    checks apply to any contract that declares it — including one at `LOW`, where the bar
    is only 2 distinct users but a campaign with a single user on a single day still
    suppresses. Folding them together meant a voluntary declaration went entirely
    unchecked.
    """
    if contract.suppression is None:
        return []

    findings: list[Finding] = []
    declared = set(contract.target_columns)
    # The discriminator is required unconditionally; the two diagnostics only where the fact has a
    # single writer. On a conformed fact they are refused instead, by
    # `check_delivery_diagnostics_are_not_conformed` — a per-delivery value has no single meaning
    # at a row three deliveries assembled. That rule needs the whole contract set, so this one
    # checks only what one contract can settle on its own.
    # A closed-enumeration contract discards suppressed rows before its fact write and carries
    # absence in `cell_state` (ADR-099 point 7), so it stores no discriminator.
    # An SCD2 dimension that stores no discriminator discards its withheld rows the same way and
    # holds no measure a withheld row could read as zero: the history keeps the omitted key's
    # earlier version (packs 9 and 13, ADR-103).
    if contract.suppression.discriminator_column not in declared and not (
        contract.records_cell_state or contract.discards_into_history
    ):
        findings.append(
            Finding(
                rule="suppression-in-schema",
                severity=Severity.ERROR,
                contract_id=contract.contract_id,
                message=(
                    f"{contract.suppression.discriminator_column} is declared as the suppression "
                    "discriminator but not mapped into the target schema. It is part of the "
                    "schema, not a diagnostic: without it zero is indistinguishable from "
                    "suppressed."
                ),
            )
        )

    flag = contract.suppression.discriminator_column
    if flag not in contract.grain and not contract.key_unique_among_unsuppressed_only:
        findings.append(
            Finding(
                rule="suppression-collapses-key",
                severity=Severity.ERROR,
                contract_id=contract.contract_id,
                message=(
                    f"suppressed rows are nulled then re-aggregated with SUM, so rows merge "
                    f"and the declared grain collapses. Either put {flag!r} in the grain or "
                    "set key_unique_among_unsuppressed_only."
                ),
            )
        )

    return findings


def check_target_types_resolve(contract: DataContract, registry: ColumnRegistry) -> list[Finding]:
    """Every curated column must have a determinable type.

    Checked here rather than left to the generator, because a contract that cannot produce
    a table is not a valid contract — and the failure should name the author's omission at
    authoring time rather than surfacing as a generator crash later.

    Resolution is in `schema.py`: declared wins, monetary is always decimal because the
    divisor changes the type, then the registry for an AMC source column.
    """
    findings: list[Finding] = []
    for mapping in contract.fields:
        registered = (
            _lookup_in(contract, registry, _source_column(mapping.source_path))
            if mapping.resolves_against_registry
            else None
        )
        if resolve_target_type(mapping, registered) is not None:
            continue
        findings.append(
            Finding(
                rule="target-type-resolves",
                severity=Severity.ERROR,
                contract_id=contract.contract_id,
                message=(
                    f"{mapping.target_column} has no determinable type. Declare "
                    "target_type, or — for an AMC source column — record its published "
                    "type in the registry. A column with no type cannot be generated into "
                    "a table."
                ),
            )
        )
    return findings


def check_feasibility_note_is_present(contract: DataContract) -> list[Finding]:
    """Require every AMC contract to state why its grain is emittable.

    On every AMC contract, not only escalating ones. A contract whose columns are all
    `NONE` or `LOW` is in the free zone, but that is a conclusion someone has to have
    reached — and writing it down is what distinguishes having checked from having
    assumed. It is also the only place the reasoning survives a re-grain.
    """
    if not contract.is_amc or contract.grain_feasibility_note:
        return []
    return [
        Finding(
            rule="grain-feasibility-note",
            severity=Severity.ERROR,
            contract_id=contract.contract_id,
            message=(
                "every AMC contract needs a grain_feasibility_note stating why this grain "
                "is emittable given the threshold class of every column it outputs."
            ),
        )
    ]


def check_suppressed_null_is_declared(contract: DataContract) -> list[Finding]:
    """Require a SUPPRESSED null meaning wherever suppression is possible.

    If suppression is possible and no column's null carries that meaning, the null
    meanings have been collapsed somewhere — which is how a privacy withholding becomes
    a zero.

    **Vacuous when the contract has no nullable column**, and that is now reachable. A conformed
    fact does not carry the two diagnostic columns (G-28), and suppression does not null a metric
    — AMC nulls the offending *dimension* values and re-aggregates, so the merged row keeps its
    measures, as the `amc-suppression-merged` trap shows. Such a contract carries the suppression
    fact entirely in the discriminator boolean, and there is no null whose meaning could be
    collapsed. Firing anyway would be noise, and a gate that fires on correct data teaches people
    to ignore it.
    """
    if contract.suppression is None:
        return []
    if not any(mapping.nullable for mapping in contract.fields):
        return []
    # A discarding SCD2 dimension (no stored discriminator) has no measure to read as zero: its
    # nullable columns are attributes, and a withheld row never reaches it (packs 9 and 13).
    if contract.discards_into_history:
        return []
    # `not_returned` is the absence a closed-enumeration contract writes for a withheld or empty
    # cell, and it claims neither suppressed nor zero (ADR-099 point 8).
    if any(
        m.null_meaning in {NullMeaning.SUPPRESSED, NullMeaning.NOT_RETURNED}
        for m in contract.fields
    ):
        return []
    return [
        Finding(
            rule="suppressed-null-declared",
            severity=Severity.WARNING,
            contract_id=contract.contract_id,
            message=(
                "suppression columns are declared but no column declares null_meaning "
                "SUPPRESSED. Confirm no nullable column is silently reading as zero."
            ),
        )
    ]


def check_no_canonical_taxonomy_key_is_delivered(contract: DataContract) -> list[Finding]:
    """Refuse a pack that outputs a canonical taxonomy key (AC-BR.9, ADR-099 point 6).

    A pack emits source labels — `ad_product_type`, a source-set label such as `dsp+sponsored`
    — and the canonical owner resolves them through the one taxonomy (ADR-052, ADR-044). A pack
    whose SQL computes `channel_group_key` with a `CASE` is a second, unversioned copy of the
    taxonomy that drifts the day the taxonomy changes.

    Read off the delivered column names, which for a query pack are the names its SQL outputs
    (`FieldMapping.delivered_name`). A literal or load-assigned column isn't delivered, so a
    contract-stamped `channel_key: amazon_dsp` is outside this rule.
    """
    findings: list[Finding] = []
    for mapping in contract.fields:
        if mapping.has_no_delivered_value:
            continue
        delivered = mapping.delivered_name
        if delivered not in CANONICAL_TAXONOMY_COLUMNS:
            continue
        if (contract.contract_id, contract.contract_version, delivered) in (
            EXISTING_DELIVERED_TAXONOMY_KEYS
        ):
            continue
        findings.append(
            Finding(
                rule="no-canonical-taxonomy-key-delivered",
                severity=Severity.ERROR,
                contract_id=contract.contract_id,
                message=(
                    f"the delivery carries {delivered!r}, a canonical taxonomy key. A pack emits "
                    "source labels only; contracts/shared/media-taxonomy.yaml resolves them."
                ),
            )
        )
    return findings


def check_attributed_sums_carry_campaign_grain(
    contract: DataContract, registry: ColumnRegistry
) -> list[Finding]:
    """Refuse an attributed-events additive sum above campaign grain (AC-BR.9, ADR-099 point 6).

    Each attributed-events row pairs one conversion with the one traffic event it is credited
    to, and neither table carries a conversion ID, so one purchase may sit on several campaigns'
    rows. A sum of a metric is safe only while the grain still holds `campaign_id_string`.
    Above it, additive outcomes come from `conversions`. A distinct count (`non_additive`) is
    outside this rule: counting shoppers once is exactly what a distinct count does.
    """
    attributed = ATTRIBUTED_EVENT_TABLES & set(contract.source_tables)
    if not contract.is_amc or not attributed:
        return []
    grain = set(contract.grain)
    if any(
        mapping.target_column in grain
        and mapping.resolves_against_registry
        and _source_column(mapping.source_path) == CAMPAIGN_SOURCE_COLUMN
        for mapping in contract.fields
    ):
        return []

    findings: list[Finding] = []
    for mapping in contract.fields:
        if mapping.target_column in grain or mapping.non_additive:
            continue
        if mapping.is_derived:
            inputs = tuple(
                pair
                for entry in mapping.derived_from
                for pair in _derived_input_columns(contract, registry, entry)
            )
        elif mapping.resolves_against_registry:
            inputs = _derived_input_columns(contract, registry, _source_column(mapping.source_path))
        else:
            continue
        summed = sorted(
            {
                f"{table}.{column.name}"
                for table, column in inputs
                if table in attributed and column.is_metric
            }
        )
        if summed:
            findings.append(
                Finding(
                    rule="attributed-additive-needs-campaign-grain",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{mapping.target_column} sums {', '.join(summed)} at a grain without "
                        f"{CAMPAIGN_SOURCE_COLUMN}. One purchase can be credited to several "
                        "campaigns there, so above campaign grain additive outcomes come from "
                        "conversions. Qualify a shared column as table.column if it is read "
                        "from another table."
                    ),
                )
            )
    return findings


def _name_tokens(name: str) -> set[str]:
    """Split one source name into lowercase words across `_`, `.`, `[]` and camelCase."""
    return {token.lower() for token in _NAME_TOKEN.findall(name)}


def check_no_source_reported_ratio_is_mapped(contract: DataContract) -> list[Finding]:
    """Refuse a source-reported ratio or share mapped to a curated column (AC-BR.10, ADR-105).

    A curated fact holds levels only. A share a source computed, such as Brand Analytics'
    repeat-customer share, can't be rebuilt from the levels we hold and invites being multiplied
    into a count, so it stays in the source-aligned layer, which every source field reaches
    without a contract mapping. `check_no_stored_ratios` reads the curated name; this reads the
    source name, so renaming a share on the way in doesn't get it through.

    Every segment of a source path is read, because a vendor value's leaf is often `amount`.
    A derived value is judged by the columns it is computed from.
    """
    findings: list[Finding] = []
    for mapping in contract.fields:
        if mapping.has_no_delivered_value or mapping.from_extraction_option:
            continue
        names = mapping.derived_from if mapping.is_derived else (mapping.source_path,)
        offending = sorted(
            {token for name in names for token in _name_tokens(name) & SOURCE_RATIO_TOKENS}
        )
        if offending:
            findings.append(
                Finding(
                    rule="no-source-reported-ratio",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"{mapping.target_column} maps {', '.join(names)}, which reads as a "
                        f"source-reported ratio or share ({', '.join(offending)}). A curated "
                        "fact holds levels only; the share stays in the source-aligned table."
                    ),
                )
            )
    return findings


def validate(contract: DataContract, registry: ColumnRegistry) -> tuple[Finding, ...]:
    """Run every rule and return all findings, worst first.

    Every rule runs regardless of earlier failures: naming all the gaps at once is the
    difference between one correction and an afternoon of them.
    """
    findings: list[Finding] = []
    findings.extend(check_money_is_never_bare(contract))
    findings.extend(check_currency_agreement_is_declared(contract))
    findings.extend(check_no_stored_ratios(contract))
    findings.extend(check_grain_not_in_table_name(contract))
    findings.extend(check_grain_columns_are_mapped(contract))
    findings.extend(check_amc_columns_are_verified(contract, registry))
    findings.extend(check_derived_inputs_are_usable(contract, registry))
    findings.extend(check_divisors_match_registry(contract, registry))
    findings.extend(check_suppression_is_declared(contract, registry))
    findings.extend(check_suppression_declaration_is_consistent(contract))
    findings.extend(check_target_types_resolve(contract, registry))
    findings.extend(check_feasibility_note_is_present(contract))
    findings.extend(check_suppressed_null_is_declared(contract))
    findings.extend(check_no_canonical_taxonomy_key_is_delivered(contract))
    findings.extend(check_attributed_sums_carry_campaign_grain(contract, registry))
    findings.extend(check_no_source_reported_ratio_is_mapped(contract))
    return tuple(
        sorted(findings, key=lambda f: (f.severity is not Severity.ERROR, f.rule, f.message))
    )


def check_shared_measures_are_separable(
    contracts: Sequence[DataContract],
) -> tuple[Finding, ...]:
    """Check that two contracts sharing a measure on one fact can be told apart by the grain.

    **The only rule here that takes the whole contract set**, because it is about a relationship
    between contracts rather than a property of one. Everything else `validate` runs is per
    contract, which is why this is called separately.

    On a fact with one writer there is nothing to separate. On `fct_ad_exposure` there are four,
    and `amc-dsp-impressions` and `amc-reach-frequency` both supply `impressions` and `spend`. Two
    distinct failures follow if they cannot be distinguished, and this rule catches both:

    - **The withdraw over-reaches.** A load nulls its columns across the partition, so it takes the
      other contract's figures. Observed live before ADR-033's predicate was narrowed.
    - **The merge collides.** Two rows at an identical grain, each claiming the same measure, and
      the later load silently overwrites the earlier.

    Separable means their `owned_grain_values` are disjoint on at least one grain column *both*
    bound. A column only one of them bounds proves nothing: the unbounded side may produce the
    same value.
    """
    findings: list[Finding] = []
    by_table: dict[str, list[DataContract]] = {}
    for contract in contracts:
        if contract.targets_a_fact:
            by_table.setdefault(contract.target_table, []).append(contract)

    for table, writers in sorted(by_table.items()):
        if len(writers) < SEVERAL_WRITERS:
            continue
        for first, second in combinations(sorted(writers, key=lambda c: c.contract_id), 2):
            shared = _shared_columns(first, second)
            if not shared:
                continue
            if _separable(first, second):
                continue
            findings.append(
                Finding(
                    rule="shared_measures_are_separable",
                    severity=Severity.ERROR,
                    contract_id=f"{first.contract_id} + {second.contract_id}",
                    message=(
                        f"both write {', '.join(shared)} on {table}, and no grain column "
                        "distinguishes them. Declare value_domain on a grain column each maps "
                        "from a source column, or stamp a distinguishing literal. Without it a "
                        "load withdraws the other's figures across the partition, and their rows "
                        "collide at an identical grain."
                    ),
                )
            )
    return tuple(findings)


def check_delivery_diagnostics_are_not_conformed(
    contracts: Sequence[DataContract],
) -> tuple[Finding, ...]:
    """Refuse a per-delivery diagnostic on a fact several contracts write.

    **Takes the whole contract set**, because whether a fact is conformed is a property of how
    many contracts target it.

    AMC's two diagnostic columns describe *one delivery's view of a row*, not the row. The reason
    column proves it in its own format — `97 'dsp_impressions' users < 100 required` names the
    data source it is about ([amc.md](../../../docs/sources/amc.md) §5, `[verified]`). The count
    is capped per query at 2 or 100 by the strictest output column, and AMC leaves it blank on a
    row it merged. Neither is a fact about a row that three deliveries assembled.

    So a conformed fact holds one column where three deliveries each have something different to
    say, and the write path cannot resolve it: the three DSP contracts stamp identical channel
    literals, so their withdrawals overlap exactly and each erases the others' values rather than
    superseding them. Erased, not stale — and `distinct_user_count` declares
    `null_meaning: suppressed`, so the erasure is indistinguishable from AMC withholding the count.

    **The discriminator is exempt and must stay.** It belongs in the grain, where a suppressed row
    and an unsuppressed one are different rows, so the three writers never contend for it. It is
    also the only one of the three that makes zero distinguishable from suppressed, which is the
    property [canonical-model](../../../docs/canonical-model.md) §4 depends on.

    The diagnostics are still *requested* — the `suppression` block is unchanged, so they arrive in
    the delivery and `check_header` still proves it. They simply are not conformed into the table.
    Per-row, per-dataset detail stays in the retained raw delivery, which is where it is already
    partitioned by dataset.
    """
    findings: list[Finding] = []
    by_table: dict[str, list[DataContract]] = {}
    for contract in contracts:
        if contract.targets_a_fact:
            by_table.setdefault(contract.target_table, []).append(contract)

    for table, writers in sorted(by_table.items()):
        if len(writers) < SEVERAL_WRITERS:
            continue
        for contract in sorted(writers, key=lambda c: c.contract_id):
            declared = set(contract.target_columns)
            offending = sorted(declared.intersection(_diagnostics_of(contract)))
            if not offending:
                continue
            findings.append(
                Finding(
                    rule="delivery_diagnostics_are_not_conformed",
                    severity=Severity.ERROR,
                    contract_id=contract.contract_id,
                    message=(
                        f"maps {', '.join(offending)} into {table}, which "
                        f"{len(writers)} contracts write. A per-delivery diagnostic has no single "
                        "value at a row several deliveries assembled, and each load erases the "
                        "others' rather than superseding them. Keep requesting it in the "
                        "suppression block and remove the field mapping; the per-row detail stays "
                        "in the raw delivery."
                    ),
                )
            )
    return tuple(findings)


def _diagnostics_of(contract: DataContract) -> frozenset[str]:
    """Name the suppression columns that describe a delivery rather than a row.

    The discriminator is deliberately absent: it is a fact about the row, it lives in the grain,
    and it is what the read path folds over a group.
    """
    if contract.suppression is None:
        return frozenset()
    return frozenset(
        {contract.suppression.reason_column, contract.suppression.distinct_user_count_column}
    )


def _shared_columns(first: DataContract, second: DataContract) -> tuple[str, ...]:
    """Give the non-grain columns both contracts write, the ones that can collide.

    Three exclusions apply. **Grain columns** are how contracts are told apart, so sharing one is
    the mechanism rather than the problem. **Conformed provenance** is projected identically by
    every contract writing a fact, so agreement is guaranteed. **Conformed taxonomy attributes**
    are governed context functionally determined by grain media identity, not measures contributed
    independently by each source dataset.

    Extraction-option columns were exempted here and are not any more. That exemption existed
    because AMC synthesises `suppression_reason` and `distinct_user_count` into every delivery, so
    every contract carried them and any two contracts on one fact necessarily shared them — which
    made the rule unsatisfiable rather than false. `check_delivery_diagnostics_are_not_conformed`
    removes the cause instead: a fact with several writers does not carry them at all, so nothing
    needs looking away from. See G-28 in docs/decision-coverage-registry.md.
    """
    grain = set(first.grain) | set(second.grain)
    common = set(first.target_columns) & set(second.target_columns)
    return tuple(
        sorted(common - grain - set(CONFORMED_PROVENANCE_COLUMNS) - _CONFORMED_TAXONOMY_ATTRIBUTES)
    )


def _separable(first: DataContract, second: DataContract) -> bool:
    """Whether some grain column both contracts bound proves their rows cannot be the same row."""
    mine, theirs = first.owned_grain_values, second.owned_grain_values
    return any(
        not set(mine[column]).intersection(theirs[column]) for column in set(mine) & set(theirs)
    )


def has_errors(findings: tuple[Finding, ...]) -> bool:
    """Whether any finding blocks the gate."""
    return any(f.severity is Severity.ERROR for f in findings)
