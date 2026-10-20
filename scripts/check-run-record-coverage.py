# SPDX-License-Identifier: MIT-0
"""Refuse a Makefile test or scan target that bypasses the run recorder.

Every test and scan entry point writes an immutable run record (docs/test-run-records.md), so a
claim that something passed can cite the record of that exact tree. A target is a test or scan
target when its name matches TEST_TARGET, or when its recipe runs a test runner or scanner
(RUNNER). It passes only when its recipe runs a recorder (RECORDER): `scripts/run_record.py run`
or `gate`, the publication or release gate, or `scripts/mutation.py run|next`. EXEMPT names the matched targets that write no record, each with
its reason; an exemption that no longer matches a test or scan target fails too, so the list
cannot go stale.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"
TEST_TARGET = re.compile(
    r"^(test|test-.+|.+-test|verify|verify-clean|accept-.+|regress-.+|mutate|mutate-.+"
    r"|.+-gate)$"
)
RUNNER = re.compile(
    r"\b(pytest|jest|vitest|npm (?:run )?test|cosmic-ray|stryker|ash scan|gitleaks|pip-audit"
    r"|npm audit|vulture)\b"
)
RECORDER = re.compile(
    r"scripts/run_record\.py (?:run|gate)\b"
    r"|scripts/(?:publication|release)_gate\.py\b"
    r"|scripts/mutation\.py (?:run|next)\b"
)
EXEMPT: dict[str, str] = {
    "dead-code-allowlist": "regenerates scripts/vulture-allowlist.py for review; reports no result",
}
TARGET_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?![:=])")


@dataclass(frozen=True)
class Target:
    """One Makefile rule: its name, its line and its recipe text."""

    name: str
    line: int
    recipe: str


def targets(text: str) -> list[Target]:
    """Return every rule of a Makefile with the tab-indented recipe lines that follow it."""
    found: list[Target] = []
    current: tuple[str, int] | None = None
    recipe: list[str] = []
    in_define = False

    def close() -> None:
        if current is not None:
            found.append(Target(current[0], current[1], "\n".join(recipe)))

    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("define "):
            in_define = True
            continue
        if in_define:
            in_define = line.strip() != "endef"
            continue
        if line.startswith("\t") and current is not None:
            recipe.append(line)
            continue
        match = TARGET_LINE.match(line)
        if match and match.group(1) != ".PHONY":
            close()
            current, recipe = (match.group(1), number), []
    close()
    return found


def is_test_or_scan(target: Target) -> bool:
    """Return whether the target runs tests or a scan, by its name or by its recipe."""
    return bool(TEST_TARGET.match(target.name) or RUNNER.search(target.recipe))


def findings(text: str, exempt: dict[str, str] = EXEMPT) -> list[str]:
    """Return one line per unrecorded test or scan target and per stale exemption."""
    rules = targets(text)
    matched = {t.name for t in rules if is_test_or_scan(t)}
    problems = [
        f"Makefile:{t.line}: {t.name} runs tests or a scan without the run recorder"
        for t in rules
        if t.name in matched and t.name not in exempt and not RECORDER.search(t.recipe)
    ]
    problems += [
        f"EXEMPT names {name}, which is not a test or scan target in the Makefile"
        for name in sorted(set(exempt) - matched)
    ]
    return problems


def main() -> int:
    """Print every finding and return nonzero when any test or scan target is unrecorded."""
    text = MAKEFILE.read_text(encoding="utf-8")
    problems = findings(text)
    for problem in problems:
        print(problem)  # noqa: T201
    if problems:
        print(f"RUN RECORD COVERAGE FAILED: {len(problems)} finding(s)")  # noqa: T201
        return 1
    count = sum(1 for t in targets(text) if is_test_or_scan(t))
    print(  # noqa: T201
        f"RUN RECORD COVERAGE PASSED: {count} test or scan target(s), {len(EXEMPT)} exempt"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
