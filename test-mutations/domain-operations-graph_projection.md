# Mutation review — src/domain/operations/graph_projection.py
- Commit: 4c3c73153ff93ad11fcb6db8469d6e5e7f30f477
- Date: 2026-10-15
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/operations/graph_projection.py TESTS='tests/unit/test_flow_explain.py tests/unit/test_operational_status_metrics.py'`
- Mutants: 222 generated, 141 killed, 81 survived
- Survivors: 81 open, 0 killed since, 0 equivalent
This is a no-remediation report taken at the rolling-freshness convergence tip (plan item 9). Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.
## S-01 · OPEN · GraphProjectionConflictError.__init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:154
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-02 · OPEN · epoch_label mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:159
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:162
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-04 · OPEN · _epoch mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:203
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-05 · OPEN · _epoch mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:205
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-06 · OPEN · _execution_row mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:228
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-07 · OPEN · _execution_row mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:231
- Mutants: 5 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-08 · OPEN · _execution_row mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:243
- Mutants: 8 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-09 · OPEN · _execution_row mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:245
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, ReplaceOrWithAnd, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-10 · OPEN · _execution_row mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:256
- Mutants: 8 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-11 · OPEN · project_graph_item mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:269
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-12 · OPEN · project_graph_item mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:275
- Mutants: 2 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-13 · OPEN · project_graph_item mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:277
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-14 · OPEN · project_graph_item mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:279
- Mutants: 8 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-15 · OPEN · project_graph_item mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:290
- Mutants: 4 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-16 · OPEN · project_graph_item mutation survivor
- Track: mechanical
- Location: src/domain/operations/graph_projection.py:293
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
