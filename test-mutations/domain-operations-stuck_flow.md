# Mutation review — src/domain/operations/stuck_flow.py
- Commit: 4c3c73153ff93ad11fcb6db8469d6e5e7f30f477
- Date: 2026-10-15
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/operations/stuck_flow.py TESTS='tests/unit/test_stuck_flow_detector.py'`
- Mutants: 315 generated, 238 killed, 77 survived
- Survivors: 77 open, 0 killed since, 0 equivalent
This is a no-remediation report taken at the rolling-freshness convergence tip (plan item 9). Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.
## S-01 · OPEN · SweepIncompleteError.__init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:111
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:114
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-03 · OPEN · DueObservation.kind mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:134
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:137
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:146
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:154
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-07 · OPEN · promotion_age_seconds mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:203
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-08 · OPEN · promotion_age_seconds mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:204
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-09 · OPEN · promotion_age_seconds mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:211
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-10 · OPEN · bake_contribution_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:237
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-11 · OPEN · bake_contribution_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:239
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-12 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:244
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-13 · OPEN · _count_bake mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:254
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-14 · OPEN · _count_bake mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:262
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-15 · OPEN · _count_bake mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:265
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-16 · OPEN · _count_bake mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:274
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-17 · OPEN · _count_bake mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:275
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_IsNot, ReplaceComparisonOperator_Gt_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-18 · OPEN · sweep mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:282
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-19 · OPEN · sweep mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:286
- Mutants: 22 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-20 · OPEN · sweep mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:302
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Sub_BitXor`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-21 · OPEN · sweep mutation survivor
- Track: mechanical
- Location: src/domain/operations/stuck_flow.py:313
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
