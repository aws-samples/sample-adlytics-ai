# Mutation review — src/adapters/inbound/tool_handlers/catalog_entrypoint.py

- Commit: 6b8dce224782dc9eaa0d3d35074c8c13acc65ecf
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/tool_handlers/catalog_entrypoint.py TESTS='tests/unit/test_catalog_entrypoint.py'`
- Mutants: 37 generated, 34 killed, 3 survived
- Survivors: 3 open, 0 killed since, 0 equivalent

This retained re-mutation follows Wave 1 adversarial test remediation. Each remaining survivor requires semantic/equivalence triage before further test changes.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/tool_handlers/catalog_entrypoint.py:58
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/tool_handlers/catalog_entrypoint.py:59
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/tool_handlers/catalog_entrypoint.py:60
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN
