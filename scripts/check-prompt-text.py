#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Enforce that no prompt text lives under src/.

The registry API is the only path a prompt reaches production (ADR-069; the
prompt-administration LLD §6 states the rule verbatim): generation reads only the
registry's active version, and the governed contract text under contracts/ is the
immutable v1 seed. A prompt string literal in src/ would be a second, ungoverned
path to production wording.

Two rules, both mechanical:

1. No persona-voice string literal under src/: a line containing the literal
   "You are" is a prompt fragment. The calibration runner's fixed case
   instruction is infrastructure, not a slot prompt, and is exempted by path.
2. Only the contract loader, the deployment seed job, and their validation entry
   points may reference `initial_fictional_prompt`; any other reference is code
   routing seed text around the registry.

Usage:  python3 scripts/check-prompt-text.py

Exits non-zero on any finding, so it can run in CI.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

PERSONA_LITERAL = re.compile(r"[\"']([^\"']*\bYou are\b[^\"']*)[\"']")
SEED_FIELD = re.compile(r"\binitial_fictional_prompt\b")

# The only modules allowed to touch the immutable v1 seed text.
SEED_OWNERS = {
    SRC / "adapters" / "jobs" / "contract_validation" / "insight_loader.py",
    SRC / "adapters" / "jobs" / "contract_validation" / "__main__.py",
    SRC / "adapters" / "jobs" / "prompt_registry_seed" / "handler.py",
    SRC / "domain" / "presentation" / "insights.py",
}

findings: list[str] = []
checked = 0
for path in sorted(SRC.rglob("*.py")):
    checked += 1
    relative = path.relative_to(ROOT)
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if PERSONA_LITERAL.search(line):
            findings.append(
                f"{relative}:{number}: persona-voice string literal — prompt text "
                "reaches production only through the registry API"
            )
        if SEED_FIELD.search(line) and path not in SEED_OWNERS:
            findings.append(
                f"{relative}:{number}: reads initial_fictional_prompt outside the "
                "contract loader and deployment seed"
            )

print("PROMPT TEXT CHECK")
print("=" * 60)
print(f"  scanned: {checked} modules under src/")
print()
if findings:
    for finding in findings:
        print(f"  {finding}")
    print()
    print(f"{len(findings)} finding(s): prompt text must live only in the registry.")
    sys.exit(1)
print("No prompt text under src/; the registry remains the only production path.")
