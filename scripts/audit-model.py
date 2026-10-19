#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Cross-document consistency audit for the canonical model.

Checks that model identifiers and domain assignments agree across the docs that
mention them, and that naming rules hold. Exits non-zero on any finding, so it
can run in CI.

Usage:  python3 scripts/audit-model.py
"""

import pathlib
import re
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INTERNAL = ROOT / "internal"

# docs/ is audited, plus local working documents when present. The ownership rule
# is that no document may name a model object that canonical-model.md does not
# define, and local planning documents name plenty; skipping them would drop them
# from the check while still reporting success.
SCAN_DIRS = (DOCS, INTERNAL)

OWNER = DOCS / "canonical-model.md"  # owns field-level definitions
APPROACH = (
    DOCS / "capabilities" / "data-layer" / "architecture.md"
)  # owns bus matrix + naming rules
DECISION = DOCS / "decisions/adr-003-canonical-model-decomposition.md"

IDENT = re.compile(r"\b((?:dim|fct|snap|stg|int)_[a-z0-9_]+)\b")
FACT_PREFIXES = ("fct_", "snap_")

DOMAINS = {
    "exposure",
    "conversion",
    "retail_demand",
    "retail_availability",
    "shopper_lifecycle",
    "incrementality",
    "cross_channel_performance",
}

findings = []
notes = []


def fail(category, msg):
    findings.append((category, msg))


def note(msg):
    notes.append(msg)


def counter_examples(text):
    """Identifiers written deliberately as bad examples in naming rules.

    The docs teach by contrast — "`dim_product` ✓ · `dim_asin` ✗" and
    "`fct_retail_sales`, not `fct_vendor_central_sales`". Those must not be
    mistaken for real model objects.
    """
    bad = set()
    for pat in (
        r"not\s+`([a-z0-9_]+)`",  # "..., not `fct_x`"
        r"`([a-z0-9_]+)`\s*✗",  # "`fct_x` ✗"
        r"✗\s*`([a-z0-9_]+)`",  # "✗ `fct_x`"
    ):
        bad.update(re.findall(pat, text))
    return bad


def idents_in(path, drop_counter_examples=True):
    if not path.exists():
        return set()
    text = path.read_text()
    ids = set(IDENT.findall(text))
    if drop_counter_examples:
        ids -= counter_examples(text)
    return ids


# ---------------------------------------------------------------- collect
per_doc = {}
for d in SCAN_DIRS:
    for f in sorted(d.rglob("*.md")):
        ids = idents_in(f)
        if ids:
            per_doc[f.relative_to(ROOT)] = ids

all_ids = set().union(*per_doc.values()) if per_doc else set()
owner_ids = idents_in(OWNER)

facts = {i for i in all_ids if i.startswith(FACT_PREFIXES)}
dims = {i for i in all_ids if i.startswith("dim_")}
staging = {i for i in all_ids if i.startswith(("stg_", "int_"))}

# ------------------------------------------------- 1. ownership
# every fact and dimension must be defined in the owning doc
for i in sorted(facts | dims):
    if i not in owner_ids:
        where = [str(d) for d, s in per_doc.items() if i in s]
        fail(
            "ownership",
            f"{i} is referenced in {', '.join(where)} but absent from canonical-model.md",
        )

# ------------------------------------------------- 2. naming rules
GRAIN_TOKENS = ("_day_", "_daily_", "_weekly_", "_monthly_", "_hour_")
for i in sorted(facts | dims):
    if any(t in i for t in GRAIN_TOKENS) or i.endswith(("_day", "_daily", "_hour")):
        fail("naming", f"{i} appears to encode grain in its name")
    if i.startswith("dim_") and "asin" in i:
        fail("naming", f"{i} embeds a channel-specific identifier in a conformed dimension name")

# channel tokens are only allowed as a subtype suffix on a fact
CHANNEL_TOKENS = ("amazon", "vendor_central", "walmart", "criteo")
for i in sorted(dims):
    for tok in CHANNEL_TOKENS:
        if tok in i:
            fail(
                "naming",
                f"{i} contains channel token '{tok}' — conformed dimensions must be channel-neutral",
            )
for i in sorted(facts):
    for tok in CHANNEL_TOKENS:
        if tok in i and not i.endswith("_" + tok):
            fail("naming", f"{i} contains channel token '{tok}' not as a subtype suffix")

# staging is the only place source names may appear
for i in sorted(staging):
    if "__" not in i:
        note(f"{i} is a staging model without a '__' source separator")

# ------------------------------------------------- 3. bus matrix coverage
approach = APPROACH.read_text() if APPROACH.exists() else ""
matrix_rows = set(re.findall(r"^\|\s*`((?:fct|snap)_[a-z0-9_]+)`", approach, re.M))
owner_facts = {i for i in owner_ids if i.startswith(FACT_PREFIXES)}

SUBTYPE = tuple("_" + t for t in CHANNEL_TOKENS)

# The bus matrix documents drill-across conformance. A reconciliation control total is
# never a drill-across target — its only consumer is a DQ comparison against the fact it
# checks — so it carries no row. The exempt set is read from canonical-model.md's own
# "Reconciliation facts" section rather than hardcoded here, so adding one to the model is
# the only step needed.
owner_text = OWNER.read_text()
reconciliation = set()
if "### Reconciliation facts" in owner_text:
    section = owner_text.split("### Reconciliation facts", maxsplit=1)[1].split(
        "\n### ", maxsplit=1
    )[0]
    reconciliation = set(re.findall(r"`((?:fct|snap)_[a-z0-9_]+)`", section))

for f_ in sorted(owner_facts - matrix_rows):
    if f_ in reconciliation:
        note(
            f"{f_} is a reconciliation control total — never drilled across, no bus matrix row expected"
        )
        continue
    if f_.endswith(SUBTYPE):
        note(
            f"{f_} is a channel subtype fact — reached by drill-across, no bus matrix row expected"
        )
        continue
    fail(
        "bus-matrix",
        f"{f_} is defined in canonical-model.md but has no bus matrix row in Data Layer architecture",
    )
for f_ in sorted(matrix_rows - owner_facts):
    fail("bus-matrix", f"{f_} has a bus matrix row but is not defined in canonical-model.md")


# ------------------------------------------------- 4. domain agreement
def domain_map(path):
    """Extract fact -> domain from a markdown table with a domain column."""
    out = defaultdict(set)
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`*") for c in line.split("|")[1:-1]]
        found_domain = next((c for c in cells if c in DOMAINS), None)
        if not found_domain:
            continue
        for c in cells:
            for m in IDENT.findall(c):
                if m.startswith(FACT_PREFIXES):
                    out[m].add(found_domain)
    return out


d_owner = domain_map(OWNER)
d_appr = domain_map(APPROACH)
d_dec = domain_map(DECISION)

for f_ in sorted(set(d_owner) | set(d_appr) | set(d_dec)):
    assigned = {
        "canonical-model": d_owner.get(f_, set()),
        "data-layer": d_appr.get(f_, set()),
        "adr-003": d_dec.get(f_, set()),
    }
    stated = {k: v for k, v in assigned.items() if v}
    values = {frozenset(v) for v in stated.values()}
    if len(values) > 1:
        detail = "; ".join(f"{k}={sorted(v)}" for k, v in stated.items())
        fail("domain", f"{f_} assigned to different domains across docs: {detail}")

# ------------------------------------------------- 5. unverified fields
owner_text = OWNER.read_text() if OWNER.exists() else ""
unverified = owner_text.count("| ? |")
note(f"{unverified} field(s) marked unverified (?) in canonical-model.md")

# ------------------------------------------------- report
print("CANONICAL MODEL AUDIT")
print("=" * 60)
print(f"  dimensions:  {len(dims)}")
print(f"  facts:       {len(facts)}  ({len([f for f in facts if f.startswith('snap_')])} snapshot)")
print(f"  bus matrix:  {len(matrix_rows)} rows")
print()

if notes:
    print("NOTES")
    for n in notes:
        print(f"  · {n}")
    print()

if findings:
    print(f"FINDINGS ({len(findings)})")
    by_cat = defaultdict(list)
    for cat, msg in findings:
        by_cat[cat].append(msg)
    for cat in sorted(by_cat):
        print(f"\n  [{cat}]")
        for m in by_cat[cat]:
            print(f"    ✗ {m}")
    sys.exit(1)

print("No consistency findings. Naming, ownership, bus matrix and domain")
print("assignment agree across canonical-model.md, Data Layer architecture and ADR-003.")
