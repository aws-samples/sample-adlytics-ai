# Mutation review — src/adapters/jobs/evaluation_writer/span.py

- Commit: 770518a612a031c0238b73562e729f8e850c5b4b
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/jobs/evaluation_writer/span.py TESTS='tests/unit/test_evaluation_writer_contract.py'`
- Mutants: 50 generated, 42 killed, 8 survived
- Survivors: 8 open, 0 killed since, 0 equivalent

This final local boundary proof follows invariant-driven test remediation. Remaining survivors require semantic/equivalence triage.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/jobs/evaluation_writer/span.py:15
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-02 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/jobs/evaluation_writer/span.py:28
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_Eq`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-03 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/jobs/evaluation_writer/span.py:30
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-04 · OPEN · released_span_json mutation survivor
- Track: mechanical
- Location: src/adapters/jobs/evaluation_writer/span.py:71
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN
