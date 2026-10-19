#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Ban the telemetry APIs the repository's observability design replaces.

Owns three prohibitions that used to live as steering prose (per the steering
authoring rule: if a script can enforce it, write the script and delete the
rule): Powertools Tracer (the ADOT instrumentation path is the repository's
tracing seam), OTel baggage (the turn correlation identifier is the only
cross-boundary join key), and logger.exception (failures route through
report_error so classification and redaction are uniform).

Patterns target USAGE, not vocabulary: a docstring explaining why baggage is
excluded must not trip the gate.

Usage:  python3 scripts/check-observability-bans.py
Exits non-zero on any finding.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

BANS = [
    (
        re.compile(
            r"from\s+aws_lambda_powertools[.\w]*\s+import\s+[^\n]*\bTracer\b|aws_lambda_powertools\.Tracer\b"
        ),
        "Powertools Tracer (use the repository ADOT instrumentation path)",
    ),
    (
        re.compile(
            r"from\s+opentelemetry[.\w]*\s+import\s+[^\n]*\bbaggage\b|opentelemetry\.baggage\b"
        ),
        "OTel baggage (the turn correlation identifier is the only cross-boundary join key)",
    ),
    (
        re.compile(r"\.exception\("),
        "logger.exception (route failures through report_error)",
    ),
]

findings = []
checked = 0
for f in sorted(SRC.rglob("*.py")):
    text = f.read_text()
    checked += 1
    for pattern, why in BANS:
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            findings.append(f"{f.relative_to(ROOT)}:{line}  {why}")

print("OBSERVABILITY BAN CHECK")
print("=" * 60)
print(f"  files:  {checked} under src/")
if findings:
    print(f"FINDINGS ({len(findings)})")
    for x in findings:
        print(f"    ✗ {x}")
    sys.exit(1)
print("No banned telemetry API usage.")
