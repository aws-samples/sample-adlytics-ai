# Mutation review — src/domain/measurement/planning.py

- Commit: c27c836cb73e9fd0dc86e81cc782d054e0574ada
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/measurement/planning.py TESTS='tests/unit/test_planning.py'`
- Mutants: 711 generated, 489 killed, 222 survived
- Survivors: 222 open, 0 killed since, 0 equivalent

This retained Wave 2/3 proof follows invariant-driven test remediation. Remaining survivors require semantic/equivalence triage before additional test changes.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:93
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:100
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:256
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-04 · OPEN · FactShape mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:283
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-05 · OPEN · for_fact mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:371
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_NotEq, ReplaceComparisonOperator_Gt_Lt, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-06 · OPEN · for_fact mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:392
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-07 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:433
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-08 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:497
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-09 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:510
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-10 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:517
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-11 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:552
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-12 · OPEN · QueryPlan mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:595
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-13 · OPEN · QueryPlan mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:605
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-14 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:655
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-15 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:660
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-16 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:665
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-17 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:674
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-18 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:689
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-19 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:690
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-20 · OPEN · measure_column mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:703
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-21 · OPEN · QueryPlan mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:710
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-22 · OPEN · result_aliases mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:737
- Mutants: 2 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-23 · OPEN · _signature mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:752
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-24 · OPEN · _signature mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:754
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-25 · OPEN · _signature mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:761
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_NotEq, AddNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-26 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:768
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-27 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:784
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-28 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:787
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-29 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:793
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-30 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:803
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_LtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-31 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:804
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-32 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:828
- Mutants: 4 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-33 · OPEN · _semantic_qualifiers mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:829
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-34 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:847
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-35 · OPEN · _leaf_policies mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:852
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-36 · OPEN · _leaf_policies mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:857
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-37 · OPEN · _leaf_policies mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:861
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-38 · OPEN · _leaf_policies mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:862
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-39 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:872
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_BitXor`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-40 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:873
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-41 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:885
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-42 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:888
- Mutants: 11 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_NotEq, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Is_Gt, AddNot, ReplaceComparisonOperator_Is_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-43 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:896
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-44 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:904
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-45 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:907
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_GtE, AddNot, ReplaceComparisonOperator_Is_NotEq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-46 · OPEN · _validate_semantic_axes mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:913
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-47 · OPEN · plan mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:922
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_FloorDiv`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-48 · OPEN · plan mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1109
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, AddNot, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-49 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1113
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-50 · OPEN · _refuse_aggregating_a_distinct_count mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1169
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-51 · OPEN · _refuse_aggregating_a_distinct_count mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1171
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, ReplaceComparisonOperator_Gt_NotEq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-52 · OPEN · _measures_of mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1216
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-53 · OPEN · _simple_measures mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1234
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-54 · OPEN · _simple_measures mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1235
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-55 · OPEN · _simple_measures mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1239
- Mutants: 4 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-56 · OPEN · _simple_measures mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1241
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-57 · OPEN · _leaf_source_datasets mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1279
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-58 · OPEN · _drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1291
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_RShift`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-59 · OPEN · _drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1298
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_IsNot_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-60 · OPEN · _drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1307
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-61 · OPEN · _drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1316
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-62 · OPEN · _ratio_measures mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1327
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Gt, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-63 · OPEN · _ratio_measures mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1336
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-64 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1339
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-65 · OPEN · _additive_rollup_columns mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1361
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-66 · OPEN · _additive_rollup_columns mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1364
- Mutants: 3 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitAnd_BitXor, ReplaceBinaryOperator_BitAnd_BitOr, ReplaceBinaryOperator_BitAnd_Sub`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-67 · OPEN · _additive_rollup_columns mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1370
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-68 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1414
- Mutants: 7 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-69 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1425
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitAnd_BitOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-70 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1447
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-71 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1448
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-72 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1453
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-73 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1469
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-74 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1474
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-75 · OPEN · _plan_drill_across mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1493
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-76 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1550
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_NotEq, AddNot, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-77 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1551
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-78 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1552
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_IsNot_Lt`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-79 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1553
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-80 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1558
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-81 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1559
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq, ReplaceAndWithOr`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-82 · OPEN · _result_carries_currency mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1560
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE, ReplaceAndWithOr, ReplaceComparisonOperator_Is_LtE`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-83 · OPEN · _merged_partials mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1567
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-84 · OPEN · _merged_partials mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1568
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-85 · OPEN · _merged_partials mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1569
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN

## S-86 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/measurement/planning.py:1574
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending after the current boundary tests.
- Verdict: OPEN
