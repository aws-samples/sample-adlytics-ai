# Mutation review — src/domain/semantics/analytical_request.py

- Commit: c27c836cb73e9fd0dc86e81cc782d054e0574ada
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/semantics/analytical_request.py TESTS='tests/unit/test_resolved_analytical_request.py'`
- Mutants: 680 generated, 404 killed, 276 survived
- Survivors: 276 open, 0 killed since, 0 equivalent

This retained Wave 2/3 proof follows invariant-driven test remediation. Remaining survivors require semantic/equivalence triage before additional test changes.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:73
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:77
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-03 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:85
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:89
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:92
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-06 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:102
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-07 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:106
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-08 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:120
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-09 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:121
- Mutants: 2 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-10 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:123
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-11 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:126
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-12 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:127
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_IsNot, ReplaceComparisonOperator_Lt_Gt, ReplaceComparisonOperator_Lt_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-13 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:151
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-14 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:163
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-15 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:165
- Mutants: 4 surviving at this location
- Mutation: `NumberReplacer, ReplaceComparisonOperator_Lt_LtE, ReplaceComparisonOperator_Lt_Eq, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-16 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:169
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-17 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:177
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-18 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:189
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-19 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:197
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-20 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:205
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-21 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:213
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-22 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:226
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-23 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:234
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-24 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:237
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-25 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:244
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-26 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:252
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-27 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:253
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-28 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:254
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-29 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:255
- Mutants: 8 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_LtE, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Is`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-30 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:262
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-31 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:270
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-32 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:278
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-33 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:286
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-34 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:290
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-35 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:298
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-36 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:302
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-37 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:310
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-38 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:314
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-39 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:335
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-40 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:367
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-41 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:370
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-42 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:374
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-43 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:383
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-44 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:385
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Is, ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_GtE, ReplaceComparisonOperator_IsNot_LtE, ReplaceComparisonOperator_IsNot_Lt, AddNot, ReplaceComparisonOperator_IsNot_Eq, ReplaceComparisonOperator_IsNot_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-45 · OPEN · field mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:388
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Pow`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-46 · OPEN · field mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:390
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-47 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:393
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-48 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:401
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-49 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:409
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-50 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:418
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-51 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:426
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-52 · OPEN · _resolved_value mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:456
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceOrWithAnd, ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_IsNot_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-53 · OPEN · _stop_if_blocked mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:463
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Div`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-54 · OPEN · _metric_compatibility_blocker mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:564
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Add`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-55 · OPEN · _metric_compatibility_blocker mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:580
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-56 · OPEN · _resolve_metric mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:634
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-57 · OPEN · _resolve_metric mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:639
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-58 · OPEN · _resolve_prior_period mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:681
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_LShift`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-59 · OPEN · _resolve_prior_period mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:709
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-60 · OPEN · _resolve_prior_period mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:710
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_GtE_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-61 · OPEN · _resolve_period mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:734
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-62 · OPEN · _resolve_period mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:750
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_NotEq, NumberReplacer, ReplaceComparisonOperator_Gt_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-63 · OPEN · _closed_window_record mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:772
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE, NumberReplacer, ReplaceBinaryOperator_Sub_BitXor`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-64 · OPEN · _resolve_operation mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:826
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_Eq, NumberReplacer, NumberReplacer, ReplaceComparisonOperator_Lt_LtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-65 · OPEN · _resolve_grains mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:857
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-66 · OPEN · _resolve_grains mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:884
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-67 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:901
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-68 · OPEN · _resolve_entities mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:913
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-69 · OPEN · _resolve_entities mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:922
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-70 · OPEN · _resolve_entities mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:944
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-71 · OPEN · _resolve_entities mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:973
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-72 · OPEN · _selected_key mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:984
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_RShift`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-73 · OPEN · _selected_key mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:986
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-74 · OPEN · _convert_entity_record mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1004
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_IsNot_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-75 · OPEN · _entity_compatibility mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1024
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_RShift`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-76 · OPEN · _entity_compatibility mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1029
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-77 · OPEN · _entity_compatibility mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1031
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-78 · OPEN · _entity_compatibility mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1033
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_IsNot, AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-79 · OPEN · _entity_compatibility mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1034
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-80 · OPEN · _entity_compatibility mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1036
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-81 · OPEN · _resolve_sources mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1047
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Add`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-82 · OPEN · _resolve_sources mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1081
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-83 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1110
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-84 · OPEN · _resolve_report_lenses mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1121
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-85 · OPEN · _resolve_report_lenses mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1131
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-86 · OPEN · _resolve_report_lenses mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1141
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-87 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1191
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-88 · OPEN · _resolve_selectors mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1201
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-89 · OPEN · _resolve_selectors mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1217
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-90 · OPEN · _resolve_currency mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1274
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-91 · OPEN · _resolve_currency mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1278
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-92 · OPEN · _resolve_currency mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1285
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-93 · OPEN · _resolve_currency mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1288
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-94 · OPEN · _resolve_currency mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1289
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-95 · OPEN · _entity_label mutation survivor
- Track: mechanical
- Location: src/domain/semantics/analytical_request.py:1353
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN
