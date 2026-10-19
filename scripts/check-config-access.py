#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Enforce that configuration is read in exactly one place.

`src/adapters/config.py` resolves and validates every environment variable at
import. Any other module reading the environment directly bypasses that
validation, which means a missing or malformed value surfaces on the first
request that happens to need it rather than when the container starts.

This is checked rather than written as a steering rule, because a script can do
it and a rule someone has to remember cannot.

Usage:  python3 scripts/check-config-access.py
Exits non-zero on any finding, so it can run in CI.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OWNER = SRC / "adapters" / "config.py"

# os.environ, os.getenv, and the from-import forms that hide either one.
DIRECT_READ = re.compile(r"\bos\.environ\b|\bos\.getenv\b|\bfrom os import\b.*\b(environ|getenv)\b")

findings = []
checked = 0

for path in sorted(SRC.rglob("*.py")):
    if path == OWNER:
        continue
    checked += 1
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if DIRECT_READ.search(line):
            findings.append(
                "{}:{}: reads the environment directly — take it from "
                "adapters.config.settings() instead".format(path.relative_to(ROOT), number)
            )

print("CONFIGURATION ACCESS CHECK")
print("=" * 60)
print("  owner:   {}".format(OWNER.relative_to(ROOT)))
print("  scanned: {} other modules".format(checked))
print()

if not OWNER.exists():
    print("FINDINGS (1)")
    print("    x {} is missing — the single config owner".format(OWNER.relative_to(ROOT)))
    sys.exit(1)

if findings:
    print("FINDINGS ({})".format(len(findings)))
    for finding in findings:
        print("    x {}".format(finding))
    sys.exit(1)

print("Configuration is read only by adapters/config.py.")
