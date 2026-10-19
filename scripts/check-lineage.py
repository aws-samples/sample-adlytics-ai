#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Trace every column from source dataset to canonical model to metric.

Three artefacts have to agree and nothing checked that they did:

    contracts/*/*.yaml            what each contract claims to write
    docs/canonical-model.md       what each fact's columns actually are
    contracts/shared/
        metric-dictionary.yaml    what metrics read from those facts

`make validate-contracts` checks a contract against the AMC column *registry*, so it
catches a source column that does not exist. It cannot catch a contract writing a target
column no fact declares, a metric reading a fact nothing writes, or a metric declaring a
grain its own denominator cannot supply. Those are the errors that produce a plausible
number rather than a failure, so they are the ones worth a gate.

Runs under `uv` rather than system python, unlike the other check scripts, because it has
to parse YAML. Findings, not exceptions: a run reports every disagreement, because
discovering the next one on the following run turns one correction into an afternoon of them.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Mapping
from typing import cast

import yaml

# What a fact reference looks like is owned by the domain, so the gate and the catalog tool
# read the dictionary's expressions with one grammar rather than two regexes that drift.
from domain.catalog.capability import references_in_definition

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "docs" / "canonical-model.md"
CONTRACTS = ROOT / "contracts"
DICTIONARY = CONTRACTS / "shared" / "metric-dictionary.yaml"

SHARED_FILES = {
    "amc-columns.yaml",
    "amc-instance.yaml",
    "attribution-models.yaml",
    "audience-segment-providers.yaml",
    "canonical-publishers.yaml",
    "commerce-identities.yaml",
    "fixture-provenance.yaml",
    "iceberg-schema-compatibility.yaml",
    "media-taxonomy.yaml",
    "metric-dictionary.yaml",
    "synthetic-entity-catalog.yaml",
}

findings: list[tuple[str, str]] = []
notes: list[str] = []


def fail(category: str, message: str) -> None:
    findings.append((category, message))


def note(message: str) -> None:
    notes.append(message)


def canonical_tables() -> dict[str, set[str]]:
    """Map each fact or dimension in canonical-model.md to the columns it declares.

    Sections are `### \\`name\\`` followed by a markdown table whose first column holds
    the column name in backticks. A row can name several comma-separated columns, which
    is how the SCD2 and derived-date groups are written.
    """
    text = CANONICAL.read_text()
    tables: dict[str, set[str]] = {}
    current: str | None = None

    # h3 for a top-level fact or dimension, h4 for one nested under a grouping
    # heading such as "### Reconciliation facts".
    heading = re.compile(r"^#{3,4} `((?:dim|fct|snap)_[a-z0-9_]+)`")
    # A table row whose first cell is one or more backticked identifiers.
    row = re.compile(r"^\|\s*\*{0,2}`([^|]+?)`\*{0,2}\s*\|")

    for line in text.splitlines():
        found_heading = heading.match(line)
        if found_heading:
            current = found_heading.group(1)
            tables.setdefault(current, set())
            continue
        # A non-table heading ends the current section.
        if line.startswith("#") and current and not found_heading:
            current = None
            continue
        if current is None:
            continue
        found_row = row.match(line)
        if found_row:
            cell = found_row.group(1)
            for part in cell.split(","):
                name = part.strip().strip("`").strip()
                if re.fullmatch(r"[a-z0-9_<>]+", name):
                    tables[current].add(name)
    return tables


def contracts() -> list[tuple[pathlib.Path, dict[str, object]]]:
    """Read every contract, excluding the shared reference files."""
    found: list[tuple[pathlib.Path, dict[str, object]]] = []
    for path in sorted(CONTRACTS.rglob("*.yaml")):
        if (
            path.name in SHARED_FILES
            or path.is_relative_to(CONTRACTS / "insights")
            or path.is_relative_to(CONTRACTS / "observability")
            # The operational data-flow registry is a movement declaration with its own
            # strict loader and gate, not a data contract.
            or path.is_relative_to(CONTRACTS / "operations")
            # The governed alert rules read insight findings and write no table; their own
            # loader and validator check them against the insight contracts.
            or path.is_relative_to(CONTRACTS / "alerts")
        ):
            continue
        parsed = yaml.safe_load(path.read_text())
        if isinstance(parsed, dict):
            found.append((path.relative_to(ROOT), parsed))
    return found


def check_target_columns_exist(
    declared: dict[str, set[str]], loaded: list[tuple[pathlib.Path, dict[str, object]]]
) -> dict[str, set[str]]:
    """Every column a contract writes must be declared by its target table.

    Returns table to the set of columns some contract actually writes, for the metric
    checks below.
    """
    written: dict[str, set[str]] = {}

    for path, contract in loaded:
        table = str(contract.get("target_table", ""))
        fields = contract.get("fields") or []
        columns = {
            str(f.get("target_column"))
            for f in fields
            if isinstance(f, dict) and f.get("target_column")
        }
        # Suppression columns are declared in their own block and are part of the schema.
        suppression = contract.get("suppression")
        if isinstance(suppression, dict):
            columns |= {str(v) for v in suppression.values() if v}
        written.setdefault(table, set()).update(columns)

        if table not in declared:
            fail(
                "table-declared",
                f"{path}: target_table {table!r} is not declared in canonical-model.md. "
                "A contract cannot write a table the model does not define.",
            )
            continue

        # Dataset-identity and provenance columns are load bookkeeping rather than
        # analytical columns, so canonical-model does not enumerate them per fact.
        bookkeeping = {
            "report_type",
            "report_period",
            "period_start",
            "period_end",
            "report_options_hash",
            "retrieved_at",
            "rows_suppressed",
            "suppression_reason",
            "distinct_user_count",
        }
        unknown = sorted(columns - declared[table] - bookkeeping)
        if unknown:
            fail(
                "column-declared",
                f"{path}: writes {', '.join(unknown)} to {table}, which "
                "canonical-model.md does not declare. Either add the column to the model "
                "or stop writing it — a column in neither place is a silent schema.",
            )
    return written


DEFERRED_METRIC_FIELDS = frozenset({"metric_id", "label", "kind", "unit"})


def deferred_metric_ids(dictionary: dict[str, object]) -> set[str]:
    """Extract valid deferred metric IDs while reporting malformed deferred declarations."""
    deferred = dictionary.get("deferred", [])
    if not isinstance(deferred, list):
        fail(
            "metric-parity",
            "dictionary deferred metrics must be a list of typed metric mappings.",
        )
        return set()

    ids: set[str] = set()
    for index, entry in enumerate(cast(list[object], deferred), start=1):
        if not isinstance(entry, Mapping):
            fail(
                "metric-parity",
                f"deferred metric entry {index} must be a mapping with exactly metric_id, "
                "label, kind, unit.",
            )
            continue
        mapping = cast(Mapping[str, object], entry)
        required_fields = set(DEFERRED_METRIC_FIELDS)
        # `aliases` is optional and owned by the loader's shape check; parity here cares only
        # that every documented deferred metric is machine-readable.
        if not required_fields <= set(mapping) or not set(mapping) <= required_fields | {"aliases"}:
            fail(
                "metric-parity",
                f"deferred metric entry {index} must contain metric_id, label, kind, unit, "
                "and at most aliases.",
            )
            continue

        fields = {field: mapping[field] for field in DEFERRED_METRIC_FIELDS}
        non_string = sorted(field for field, value in fields.items() if not isinstance(value, str))
        if non_string:
            fail(
                "metric-parity",
                f"deferred metric entry {index} has non-string field(s): {', '.join(non_string)}.",
            )
            continue

        typed_fields = cast(dict[str, str], fields)
        metric_id = typed_fields["metric_id"]
        label = typed_fields["label"]
        if not metric_id.strip() or not label.strip():
            fail(
                "metric-parity",
                f"deferred metric entry {index} has a blank metric_id or label.",
            )
            continue
        ids.add(metric_id)
    return ids


def check_metric_parity(declared_in_doc: set[str], dictionary: dict[str, object]) -> None:
    """canonical-model.md section 5 and the dictionary must name the same metrics.

    The dictionary is machine-read by the curation pipeline, the tools and the Context
    Engine. The doc is what a human reads. A metric in one and not the other means three
    consumers and a reader disagree, and every number still looks plausible.
    """
    metrics = dictionary.get("metrics") or {}
    in_dictionary = set(metrics) | deferred_metric_ids(dictionary)

    only_doc = sorted(declared_in_doc - in_dictionary)
    only_dict = sorted(in_dictionary - declared_in_doc)

    if only_doc:
        fail(
            "metric-parity",
            f"in canonical-model.md section 5 but not the dictionary: {', '.join(only_doc)}. "
            "The dictionary is the machine-readable form; a doc-only metric cannot be "
            "computed.",
        )
    if only_dict:
        fail(
            "metric-parity",
            f"in the dictionary but not canonical-model.md section 5: "
            f"{', '.join(only_dict)}. A metric nobody documented is a metric nobody "
            "reviewed.",
        )


def metrics_in_doc() -> set[str]:
    """Pull the metric names out of the section 5 table."""
    text = CANONICAL.read_text()
    if "## 5. Metrics" not in text:
        fail("metric-parity", "canonical-model.md has no section 5 to compare against.")
        return set()
    section = text.split("## 5. Metrics", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    return set(re.findall(r"^\|\s*\*{0,2}`([a-z0-9_]+)`\*{0,2}\s*\|", section, re.M))


def check_metrics_resolve(written: dict[str, set[str]], dictionary: dict[str, object]) -> None:
    """Every metric must read a fact some contract writes, and a column it produces."""
    metrics = dictionary.get("metrics") or {}
    if not isinstance(metrics, dict):
        return

    for name, spec in metrics.items():
        if not isinstance(spec, dict):
            continue
        definition = str(spec.get("definition", ""))
        for table, column in references_in_definition(definition):
            if table not in written:
                fail(
                    "metric-unbacked",
                    f"metric {name} reads {table}, which no contract writes. It cannot be "
                    "computed and must be listed under `deferred:` instead.",
                )
            elif column not in written[table]:
                fail(
                    "metric-unbacked",
                    f"metric {name} reads {table}.{column}, which no contract produces.",
                )


def check_ratio_grains(dictionary: dict[str, object]) -> None:
    """A ratio cannot be valid at a grain its numerator or denominator is invalid at.

    The rule that caught three ROAS metrics claiming a product grain `ad_spend` cannot
    supply. Left unchecked, such a request passes the valid_grains allowlist and is then
    either uncomputable or computed against a broadcast denominator — a plausible wrong
    number, which is the exact failure the allowlist exists to prevent.
    """
    metrics = dictionary.get("metrics") or {}
    if not isinstance(metrics, dict):
        return

    for name, spec in metrics.items():
        if not isinstance(spec, dict) or spec.get("type") not in {"ratio", "product"}:
            continue
        own = set(spec.get("valid_grains") or [])
        operand_roles: list[tuple[str, object]] = [
            ("numerator", spec.get("numerator")),
            ("denominator", spec.get("denominator")),
        ]
        if spec.get("type") == "product":
            declared_operands = spec.get("operands") or []
            operand_roles = [
                (f"operand {index}", operand)
                for index, operand in enumerate(cast(list[object], declared_operands), start=1)
            ]
        for role, operand in operand_roles:
            if not isinstance(operand, str):
                continue
            if role == "denominator" and spec.get("delivery_ratio") == "segment_profile":
                # A C2 share's denominator is its delivery's one profiled total, the base of
                # every segment row of that delivery, paired per segment by the measurement
                # tool (design §6.5); it is never broadcast across deliveries, so it supplies
                # each segment's grain. The numerator is still checked.
                continue
            parent = metrics.get(operand)
            if not isinstance(parent, dict):
                # A ratio over a raw fact column rather than a named metric.
                continue
            parent_grains = set(parent.get("valid_grains") or [])
            over_claimed = sorted(own - parent_grains)
            if over_claimed:
                fail(
                    "ratio-grain",
                    f"metric {name} claims grain(s) {', '.join(over_claimed)} that its "
                    f"{role} {operand} does not support. A composite is only valid where "
                    "both operands are.",
                )


def main() -> int:
    print("SOURCE-TO-CANONICAL LINEAGE CHECK")
    print("=" * 60)

    declared = canonical_tables()
    loaded = contracts()
    dictionary = yaml.safe_load(DICTIONARY.read_text()) or {}

    facts = sorted(t for t in declared if t.startswith(("fct_", "snap_")))
    dims = sorted(t for t in declared if t.startswith("dim_"))
    note(f"canonical model: {len(facts)} facts, {len(dims)} dimensions")
    note(f"contracts:       {len(loaded)}")

    written = check_target_columns_exist(declared, loaded)
    check_metric_parity(metrics_in_doc(), dictionary)
    check_metrics_resolve(written, dictionary)
    check_ratio_grains(dictionary)

    for line in notes:
        print(f"  {line}")

    covered = sorted(t for t in written if t.startswith(("fct_", "snap_")))
    uncovered = [t for t in facts if t not in written]
    print(f"  facts written:   {len(covered)}/{len(facts)}")
    if uncovered:
        print(f"  no contract yet: {', '.join(uncovered)}")

    if not findings:
        print("\nEvery contract column, metric and ratio grain traces end to end.")
        return 0

    print(f"\n{len(findings)} finding(s):")
    for category, message in findings:
        print(f"  [{category}] {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
