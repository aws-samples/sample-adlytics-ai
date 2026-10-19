# Mutation review — src/adapters/inbound/tool_handlers/measurement_dispatch.py

- Commit: cea334aecd0c233863a10d61ad43741503ce3667
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/tool_handlers/measurement_dispatch.py TESTS='tests/unit/test_measurement_dispatch.py'`
- Mutants: 30 generated, 26 killed, 4 survived
- Survivors: 4 open, 0 killed since, 0 equivalent

This Wave 0 run proves local test ownership only. Each survivor requires later semantic/equivalence triage before remediation.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/tool_handlers/measurement_dispatch.py:31
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-02 · OPEN · MeasurementDispatch mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/tool_handlers/measurement_dispatch.py:42
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-03 · OPEN · MeasurementDispatch mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/tool_handlers/measurement_dispatch.py:81
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN
