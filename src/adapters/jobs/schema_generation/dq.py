# SPDX-License-Identifier: MIT-0
"""Render a DQDL ruleset per curated table.

The third projection of the contracts, alongside the Iceberg table definitions in `merge.py`
and the row models in `models.py` (docs/capabilities/data-layer/low-level-design.md §6). Feeds
build-order 1.8, the DQ gate and quarantine path.

**The unit is a table, not a contract.** Glue Data Quality evaluates a ruleset against a
table, and `glue.CfnDataQualityRuleset` takes a `targetTable`. `fct_ad_exposure` is written by
four contracts, so its rules must reflect the *merged* nullability — a column only some of
them write is nullable, and demanding completeness of it would fail a legitimate partial load.
`merge.py` has already resolved that, which is why this reads `MergedTable`.

DQDL syntax [verified 2026-08-19] against the AWS Glue developer guide: a document holds a
bracket-delimited `Rules` list, rule types are case-sensitive, and the general shape is
`<RuleType> <Parameter> <Parameter> <Expression>`.

**One documented behaviour drives a whole rule shape.** For every `ColumnValues` rule other
than `!=` and `not in`, a NULL row *fails* [verified]. On a nullable column that is exactly
wrong for this platform: a privacy-suppressed value is a legitimate absence, and quarantining
the partition that contains it would punish the source for being honest. So a `ColumnValues`
rule on a nullable column is emitted as the documented compound form,
`(ColumnValues "x" matches "…") OR (ColumnValues "x" = NULL)`. `fct_ad_exposure` needs it
today — its `currency_code` is nullable because two of its four contracts carry no money.

**What is deliberately not generated** matters as much as what is, because a rule that fires
on correct data trains people to ignore the gate:

- **No non-negativity on counts.** It looks free and it is not: the vendor documentation
  states cancellations are booked at cancellation time, so `orderedUnits` in the real-time
  sales report legitimately goes negative. No contract declares which columns may, so a
  blanket rule would assert something a source already contradicts.
- **No `RowCount`.** Whether anything arrived belongs to the freshness registry (1.9) and
  whether it arrived *complete* belongs to the control-total assertion
  (docs/capabilities/load-path/low-level-design.md §6). A third home for the same question is
  how three checks come to disagree.
- **No `Completeness "col" > threshold`** on suppressible columns. That is a volumetric claim,
  and ADR-019 settles that grain viability is measured per customer at load rather than
  declared per contract.
- **No `ColumnDataType`.** It checks whether a *string* column can be cast, and its supported
  expected types [verified] are Boolean, Date, Timestamp, Integer, Double, Float and Long —
  no Decimal and no String. The curated columns are already typed by Iceberg, so the rule
  would be both redundant and unable to express half the schema.
- **No `ColumnExists`.** The table is generated from the same contract that generates these
  rules, so a column cannot be missing without the generator being broken.
"""

from __future__ import annotations

from adapters.jobs.schema_generation.merge import MergedTable
from domain.data_contracts.contract import DataContract
from domain.shared_kernel.values import CURRENCY_PATTERN

# ISO 4217 is three uppercase letters. A currency column holding `usd`, an empty string or a
# marketplace name breaks every conversion and every rollup downstream, and unlike a count
# there is no legitimate other value — which is what makes this safe to assert where
# non-negativity is not.
"""Anchored deliberately. Whether DQDL's `matches` anchors on its own is **[unverified]**;
anchoring explicitly is correct if it does not and harmless if it does."""

SUPPRESSION_DISCRIMINATOR = "rows_suppressed"
"""The column that says a row is the product of a privacy merge. Used to scope the uniqueness
rule when a contract accepts that its key only holds among unsuppressed rows."""


def _quoted(columns: tuple[str, ...]) -> str:
    return " ".join(f'"{column}"' for column in columns)


def _completeness_rules(table: MergedTable) -> list[str]:
    """Require a value in every column the contracts say always has one.

    Derived from the merged nullability rather than each contract's, so a column two of four
    contracts write is correctly not required.
    """
    return [f'IsComplete "{column.name}"' for column in table.columns if not column.nullable]


def _uniqueness_rule(table: MergedTable, contracts: list[DataContract]) -> list[str]:
    """Assert the declared grain actually holds.

    The most valuable rule the contract can produce, because a grain is *stated* and never
    inferred — so this is the only thing that checks the statement was true. `Uniqueness`
    accepts several columns [verified], which is what makes a composite grain expressible.

    A grain may hold a nullable column (`fct_new_to_brand_buyers.ad_product_key` is NULL on
    every platform row). The Uniqueness reference page doesn't say how a NULL in a listed column
    counts [unverified, page read 2026-10-10], so this rule isn't relied on for that: the runtime
    table DQ the load path applies (`domain.semantics.table_dq`) keys on the full grain with
    NULL as an ordinary value and refuses a repeated key.
    """
    if not table.grain:
        return []

    rule = f"Uniqueness {_quoted(table.grain)} = 1.0"

    # A contract may accept that its key collapses once sub-threshold rows are nulled and
    # re-aggregated. Where it does, uniqueness holds among unsuppressed rows only, and the
    # unscoped rule would fail on data that is behaving exactly as documented. No contract
    # sets this today — every one puts the discriminator in its grain instead — but the flag
    # is part of the contract model and a ruleset that ignored it would quarantine correctly
    # merged rows.
    relaxed = any(
        contract.key_unique_among_unsuppressed_only
        for contract in contracts
        if contract.target_table == table.name
    )
    if relaxed:
        rule = f'{rule} where "{SUPPRESSION_DISCRIMINATOR} = false"'
    return [rule]


def _currency_rules(table: MergedTable, contracts: list[DataContract]) -> list[str]:
    """Check the shape of every currency column a monetary field collapses onto.

    Read from the contracts' `currency_column` declarations rather than matched by name, so a
    renamed column follows the contract instead of silently losing its rule.
    """
    declared = {
        mapping.currency_column
        for contract in contracts
        if contract.target_table == table.name
        for mapping in contract.monetary_fields
        if mapping.currency_column
    }
    nullable = {column.name for column in table.columns if column.nullable}

    rules: list[str] = []
    for name in sorted(declared):
        rule = f'ColumnValues "{name}" matches "{CURRENCY_PATTERN}"'
        if name in nullable:
            # The documented compound form. Without it every legitimately absent currency —
            # a row from a contract that carries no money — fails the rule and quarantines a
            # partition that is correct.
            rule = f'({rule}) OR (ColumnValues "{name}" = NULL)'
        rules.append(rule)
    return rules


def rules_for(table: MergedTable, contracts: list[DataContract]) -> list[str]:
    """List one table's rules, uniqueness first because it is the load-bearing one."""
    return [
        *_uniqueness_rule(table, contracts),
        *_completeness_rules(table),
        *_currency_rules(table, contracts),
    ]


def render_ruleset(table: MergedTable, contracts: list[DataContract]) -> str:
    """Render one table's DQDL document.

    Returns the `Rules = [...]` text `glue.CfnDataQualityRuleset` takes verbatim.
    """
    body = ",\n".join(f"    {rule}" for rule in rules_for(table, contracts))
    return f"Rules = [\n{body}\n]"


def runtime_rules_for(table: MergedTable, contracts: list[DataContract]) -> dict[str, object]:
    """Emit executable candidate-scoped rules from the same declarations as DQDL."""
    currency_columns = {
        mapping.currency_column
        for contract in contracts
        if contract.target_table == table.name
        for mapping in contract.monetary_fields
        if mapping.currency_column
    }
    nullable = {column.name for column in table.columns if column.nullable}
    return {
        "grain": list(table.grain),
        "requiredColumns": [column.name for column in table.columns if not column.nullable],
        "currencyColumns": [
            {"name": name, "nullable": name in nullable} for name in sorted(currency_columns)
        ],
        "uniqueAmongUnsuppressedOnly": any(
            contract.key_unique_among_unsuppressed_only
            for contract in contracts
            if contract.target_table == table.name
        ),
    }


def rulesets_for(
    tables: tuple[MergedTable, ...], contracts: list[DataContract]
) -> list[dict[str, object]]:
    """Build one ruleset entry per table, in table order.

    The physical Glue ruleset name is deliberately absent: it carries the environment, and a
    generator that does not know which environment it is generating for must not invent one.
    The V2 data stack composes it from typed configuration.
    """
    entries: list[dict[str, object]] = []
    for table in tables:
        rules = rules_for(table, contracts)
        entries.append(
            {
                "table": table.name,
                "description": (
                    f"Generated from {', '.join(table.written_by)}. "
                    "Edit the contract, not this ruleset."
                ),
                "ruleCount": len(rules),
                "ruleset": render_ruleset(table, contracts),
                "runtimeRules": runtime_rules_for(table, contracts),
            }
        )
    return entries
