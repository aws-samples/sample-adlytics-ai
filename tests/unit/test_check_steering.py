# SPDX-License-Identifier: MIT-0
"""Every fileMatch steering pattern also loads inside a git worktree.

Kiro evaluates a fileMatchPattern against the workspace root with no implicit leading `**/`, and
workflow agents edit files under `.worktrees/<name>/`, so a root-anchored pattern silently never
loads its rules for that work. A root-only literal such as `README.md` is the one exception, and
only beside its `.worktrees/*/` twin, because `**/README.md` would also load the rule for every
nested README.

Each refusal is paired with the compliant form beside it, so a guard that stopped refusing and a
guard that started refusing compliant steering both fail here. The steering front matter is read
with the steering gate's own parser, so this guard and `make check-steering` see the same patterns.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_PREFIX = ".worktrees/*/"
ANYWHERE_PREFIX = "**/"
GLOB_CHARACTERS = frozenset("*?[{")


def worktree_findings(name: str, patterns: list[str]) -> list[str]:
    """Refuse each pattern that would never match a path under `.worktrees/<name>/`."""
    findings: list[str] = []
    for pattern in patterns:
        if pattern.startswith((ANYWHERE_PREFIX, WORKTREE_PREFIX)):
            continue
        if GLOB_CHARACTERS.isdisjoint(pattern) and f"{WORKTREE_PREFIX}{pattern}" in patterns:
            continue
        findings.append(
            f"{name}: fileMatchPattern never matches inside a git worktree: {pattern}; "
            f"prefix it with {ANYWHERE_PREFIX} or pair a root literal with "
            f"{WORKTREE_PREFIX}{pattern}"
        )
    return findings


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    """Load the hyphenated steering gate as a module so its front-matter parser is reused."""
    spec = importlib.util.spec_from_file_location(
        "check_steering", ROOT / "scripts" / "check-steering.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_steering_file_match_pattern_loads_inside_a_worktree(gate: ModuleType) -> None:
    patterns: dict[str, list[str]] = {}
    for file in sorted((ROOT / ".kiro" / "steering").glob("*.md")):
        split = gate._front_matter(file.read_text())
        assert split is not None, file.name
        front_matter = gate._load_front_matter(split[0])
        if front_matter.get("inclusion") == "fileMatch":
            patterns[file.name] = list(front_matter["fileMatchPattern"])
    # An empty set would pass the check below vacuously, e.g. after a parser or path change.
    assert patterns
    findings = [f for name, entries in patterns.items() for f in worktree_findings(name, entries)]
    assert findings == []


def test_a_root_anchored_pattern_is_refused() -> None:
    assert worktree_findings("planted.md", ["src/**"]) == [
        "planted.md: fileMatchPattern never matches inside a git worktree: src/**; "
        "prefix it with **/ or pair a root literal with .worktrees/*/src/**"
    ]


def test_an_anywhere_pattern_passes() -> None:
    assert worktree_findings("planted.md", ["**/src/**"]) == []


def test_a_root_literal_without_its_worktree_twin_is_refused() -> None:
    assert worktree_findings("planted.md", ["README.md"]) == [
        "planted.md: fileMatchPattern never matches inside a git worktree: README.md; "
        "prefix it with **/ or pair a root literal with .worktrees/*/README.md"
    ]


def test_a_root_literal_beside_its_worktree_twin_passes() -> None:
    assert worktree_findings("planted.md", ["README.md", ".worktrees/*/README.md"]) == []


def test_a_root_glob_beside_its_worktree_twin_is_still_refused() -> None:
    # The twin exemption is for literals only; a glob takes the **/ prefix instead.
    assert worktree_findings("planted.md", ["src/**", ".worktrees/*/src/**"]) == [
        "planted.md: fileMatchPattern never matches inside a git worktree: src/**; "
        "prefix it with **/ or pair a root literal with .worktrees/*/src/**"
    ]
