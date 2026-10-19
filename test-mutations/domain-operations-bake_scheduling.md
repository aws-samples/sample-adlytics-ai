# Mutation review — src/domain/operations/bake_scheduling.py
- Commit: 4c3c73153ff93ad11fcb6db8469d6e5e7f30f477
- Date: 2026-10-15
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/operations/bake_scheduling.py TESTS='tests/unit/test_bake_scheduling.py'`
- Mutants: 476 generated, 383 killed, 93 survived
- Survivors: 93 open, 0 killed since, 0 equivalent
This is a no-remediation report taken at the rolling-freshness convergence tip (plan item 9). Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.
## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:41
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_Add_BitAnd, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Add_Div, ReplaceBinaryOperator_Add_FloorDiv, ReplaceBinaryOperator_Add_LShift, ReplaceBinaryOperator_Add_Mod, ReplaceBinaryOperator_Add_Mul, ReplaceBinaryOperator_Add_Pow, ReplaceBinaryOperator_Add_RShift, ReplaceBinaryOperator_Add_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:42
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_Add_BitAnd, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Add_Div, ReplaceBinaryOperator_Add_FloorDiv, ReplaceBinaryOperator_Add_LShift, ReplaceBinaryOperator_Add_Mod, ReplaceBinaryOperator_Add_Mul, ReplaceBinaryOperator_Add_Pow, ReplaceBinaryOperator_Add_RShift, ReplaceBinaryOperator_Add_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-03 · OPEN · _tenths mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:63
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-04 · OPEN · _tenths mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:65
- Mutants: 5 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, ReplaceComparisonOperator_Lt_Eq, ReplaceComparisonOperator_Lt_LtE, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-05 · OPEN · _tenths mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:68
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:73
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-07 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:86
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-08 · OPEN · task_seconds mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:109
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-09 · OPEN · task_seconds mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:111
- Mutants: 6 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, ReplaceComparisonOperator_Lt_Eq, ReplaceComparisonOperator_Lt_LtE, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-10 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:124
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-11 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:130
- Mutants: 8 surviving at this location
- Mutation: `ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow, ReplaceBinaryOperator_Sub_RShift`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-12 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:132
- Mutants: 9 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-13 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:133
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitAnd_BitOr`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-14 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:140
- Mutants: 10 surviving at this location
- Mutation: `ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_Div, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mod, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_RShift`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-15 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:141
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-16 · OPEN · two_lane_partition mutation survivor
- Track: mechanical
- Location: src/domain/operations/bake_scheduling.py:145
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
