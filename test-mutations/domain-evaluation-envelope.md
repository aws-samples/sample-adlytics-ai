# Mutation review — src/domain/evaluation/envelope.py

- Commit: 770518a612a031c0238b73562e729f8e850c5b4b
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/evaluation/envelope.py TESTS='tests/unit/test_evaluation_outbox.py tests/unit/test_evaluation_writer_contract.py'`
- Mutants: 71 generated, 48 killed, 23 survived
- Survivors: 23 open, 0 killed since, 0 equivalent

This final local boundary proof follows invariant-driven test remediation. Remaining survivors require semantic/equivalence triage.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:11
- Mutants: 6 surviving at this location
- Mutation: `NumberReplacer, ReplaceBinaryOperator_Mul_Mod, ReplaceBinaryOperator_Mul_BitOr, ReplaceBinaryOperator_Mul_BitXor, ReplaceBinaryOperator_Mul_Add, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:12
- Mutants: 7 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Add, ReplaceBinaryOperator_Mul_BitOr, NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Mul_BitXor, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:13
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:16
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-05 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:30
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-06 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:42
- Mutants: 4 surviving at this location
- Mutation: `ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Add_Sub, ReplaceBinaryOperator_Add_Mul, ReplaceBinaryOperator_Add_BitOr`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-07 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/evaluation/envelope.py:43
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN
