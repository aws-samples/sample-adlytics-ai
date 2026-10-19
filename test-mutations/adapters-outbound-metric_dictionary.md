# Mutation review — src/adapters/outbound/metric_dictionary.py

- Commit: 770518a612a031c0238b73562e729f8e850c5b4b
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/outbound/metric_dictionary.py TESTS='tests/unit/test_metric_dictionary.py'`
- Mutants: 497 generated, 373 killed, 124 survived
- Survivors: 124 open, 0 killed since, 0 equivalent

This final local boundary proof follows invariant-driven test remediation. Remaining survivors require semantic/equivalence triage.

## S-01 · OPEN · _as_mapping mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:60
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-02 · OPEN · _as_str mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:66
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-03 · OPEN · _as_int mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:72
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-04 · OPEN · _as_bool mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:83
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-05 · OPEN · _as_bool mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:84
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_NotEq`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-06 · OPEN · _optional_int mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:89
- Mutants: 12 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_Mul_Div, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_BitAnd`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-07 · OPEN · _as_str_tuple mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:93
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-08 · OPEN · _unit_kinds mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:99
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-09 · OPEN · _unit_kinds mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:117
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-10 · OPEN · _unit_kinds mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:123
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-11 · OPEN · _unit_kinds mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:124
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Is`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-12 · OPEN · _bases mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:131
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-13 · OPEN · _bases mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:143
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-14 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:170
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-15 · OPEN · _aggregation_policy mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:182
- Mutants: 2 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_BitXor`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-16 · OPEN · _aggregation_policy mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:187
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-17 · OPEN · _axis_defaults mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:203
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-18 · OPEN · _axis_defaults mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:204
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_BitXor`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-19 · OPEN · _axis_defaults mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:209
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-20 · OPEN · _axis_defaults mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:212
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-21 · OPEN · _axis_defaults mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:218
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-22 · OPEN · _axis_defaults mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:226
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-23 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:231
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-24 · OPEN · _declared_aggregation mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:280
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Mod`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-25 · OPEN · _declared_aggregation mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:285
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_LtE`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-26 · OPEN · _declared_aggregation mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:286
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-27 · OPEN · _declared_aggregation mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:291
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-28 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:310
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-29 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:338
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-30 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:345
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-31 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:347
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Lt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-32 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:363
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-33 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:364
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-34 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:372
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-35 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:386
- Mutants: 11 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_NotEq, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_GtE, AddNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-36 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:387
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-37 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:434
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-38 · OPEN · _metric_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:460
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-39 · OPEN · _inventory_categories_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:479
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-40 · OPEN · _inventory_categories_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:486
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-41 · OPEN · _deferred_metrics_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:505
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-42 · OPEN · _deferred_metrics_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:513
- Mutants: 4 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceComparisonOperator_LtE_IsNot, ReplaceComparisonOperator_LtE_IsNot`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-43 · OPEN · _deferred_metrics_from mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:518
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-44 · OPEN · load_catalog mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:557
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_IsNot_Is`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-45 · OPEN · load_catalog mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:561
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-46 · OPEN · load_catalog mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:575
- Mutants: 4 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-47 · OPEN · load_catalog mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:584
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-48 · OPEN · load_catalog mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:599
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_NotEq`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-49 · OPEN · _reject_duplicate_keys mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:635
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_BitXor`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN

## S-50 · OPEN · _reject_duplicate_keys mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/metric_dictionary.py:647
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: semantic/equivalence triage remains pending.
- Verdict: OPEN
