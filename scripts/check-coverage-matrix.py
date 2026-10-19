#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Report which recorded decisions have no test referencing them.

Answers a question nothing else in the repo can: "what verifies ADR-007?" and
its inverse, "which decisions are unverified?"

Decision IDs are collected from docs/ — they already exist and are already
stable, so no new identifier is invented. Test references are collected from
`@pytest.mark.covers(...)` markers, parsed with the AST rather than by running
pytest: faster, and no import side effects.

There is deliberately no per-test ID. The pytest node ID is the identifier and it
cannot drift from the code; a hand-assigned ID would be a second identifier for
the same thing, maintained by hand, and therefore eventually wrong.

Usage:  python3 scripts/check-coverage-matrix.py [--strict]

Exits 0 by default: unverified decisions are a finding to look at, not a failure
to block on. Pass --strict in CI once coverage is expected to be complete.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
TESTS = ROOT / "tests"

# Where each family of decision ID is declared, and the pattern that finds it.
# G and F are declared in the committed decision-coverage registry: the ID
# inventory and titles are published quality posture (as docs/security-dispositions.md
# is for reviewed scanner records), while the working narrative behind each ID lives in
# local, untracked custody.
# Tests cite all three families through pytest.mark.covers.
SOURCES: dict[str, tuple[pathlib.Path, re.Pattern[str]]] = {
    "ADR": (DOCS / "decisions", re.compile(r"^# (ADR-\d{3})", re.M)),
    "G": (DOCS / "decision-coverage-registry.md", re.compile(r"^### (G-\d{2})", re.M)),
    "F": (DOCS / "decision-coverage-registry.md", re.compile(r"^### (F-\d{2})", re.M)),
}

# Open questions (Q-NN) are deliberately excluded. An unresolved question has no
# decision behind it, so nothing to test — counting it would inflate the gap with
# items that cannot be closed by writing a test. When a Q resolves it becomes an
# ADR or a documented default, and the ADR is already tracked here.


def declared_ids() -> dict[str, set[str]]:
    """Collect every decision ID declared in docs/, grouped by family."""
    found: dict[str, set[str]] = {}
    for family, (path, pattern) in SOURCES.items():
        ids: set[str] = set()
        files = sorted(path.glob("*.md")) if path.is_dir() else [path]
        for f in files:
            if f.is_file():
                ids.update(pattern.findall(f.read_text()))
        found[family] = ids
    return found


def covered_ids() -> dict[str, list[str]]:
    """Map each decision ID to the test node IDs that claim to cover it."""
    covered: dict[str, list[str]] = {}

    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(ROOT)

        # Walk with the enclosing class in hand so the node ID is accurate.
        # `inherited` carries decision IDs from an enclosing class down to its test
        # functions, because pytest propagates a class-level marker to every item it
        # collects. Reading only function decorators made a class-level covers() marker
        # invisible here while still working for `-m` selection, so the matrix silently
        # reported tested decisions as untested.
        def visit(
            node: ast.AST,
            prefix: str,
            inherited: tuple[str, ...] = (),
            rel: pathlib.Path = rel,
        ) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    from_class = tuple(
                        decision for dec in child.decorator_list for decision in _covers_args(dec)
                    )
                    visit(child, f"{prefix}{child.name}::", inherited + from_class)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node_id = f"{rel}::{prefix}{child.name}"
                    own = tuple(
                        decision for dec in child.decorator_list for decision in _covers_args(dec)
                    )
                    for decision in dict.fromkeys(inherited + own):
                        covered.setdefault(decision, []).append(node_id)

        visit(tree, "")

        # Module-level pytestmark, e.g. pytestmark = pytest.mark.covers("ADR-007")
        for child in ast.walk(tree):
            if isinstance(child, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in child.targets
            ):
                for decision in _covers_args(child.value):
                    covered.setdefault(decision, []).append(f"{rel}::<module>")

    return covered


def _covers_args(node):  # noqa: ANN001, ANN201  (runs on system python 3.9)
    """Extract decision IDs from a covers marker.

    Handles both a bare `pytest.mark.covers(...)` and a list such as
    `pytestmark = [pytest.mark.unit, pytest.mark.covers(...)]`.
    """
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for element in node.elts:
            out.extend(_covers_args(element))
        return out
    if not isinstance(node, ast.Call):
        return []
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "covers"):
        return []
    return [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="exit non-zero on any gap")
    args = parser.parse_args()

    declared = declared_ids()
    covered = covered_ids()

    print("DECISION COVERAGE MATRIX")
    print("=" * 60)

    unknown = sorted(set(covered) - {i for ids in declared.values() for i in ids})
    total_declared = sum(len(v) for v in declared.values())
    total_covered = 0

    for family in ("ADR", "G", "F"):
        ids = sorted(declared[family])
        if not ids:
            continue
        hit = [i for i in ids if i in covered]
        total_covered += len(hit)
        print(f"\n  {family}: {len(hit)}/{len(ids)} referenced by at least one test")
        for i in ids:
            tests = covered.get(i, [])
            mark = "✓" if tests else "·"
            suffix = f"  ({len(tests)} test{'s' if len(tests) != 1 else ''})" if tests else ""
            print(f"    {mark} {i}{suffix}")

    print(f"\n  total: {total_covered}/{total_declared} decisions referenced")

    if unknown:
        print(f"\nFINDINGS ({len(unknown)})")
        for i in unknown:
            print(f"    ✗ {i} is covered by a test but declared nowhere in docs/")
        return 1

    if args.strict and total_covered < total_declared:
        print("\n  --strict: unreferenced decisions present")
        return 1

    print("\nNo unknown decision IDs. Unreferenced decisions above are a backlog,")
    print("not a failure — many are open questions with nothing yet to test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
