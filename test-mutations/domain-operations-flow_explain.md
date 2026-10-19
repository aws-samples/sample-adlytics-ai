# Mutation review — src/domain/operations/flow_explain.py
- Commit: 4c3c73153ff93ad11fcb6db8469d6e5e7f30f477
- Date: 2026-10-15
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/operations/flow_explain.py TESTS='tests/unit/test_flow_explain.py'`
- Mutants: 594 generated, 399 killed, 195 survived
- Survivors: 195 open, 0 killed since, 0 equivalent
This is a no-remediation report taken at the rolling-freshness convergence tip (plan item 9). Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.
## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:85
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:86
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:87
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:88
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:89
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:90
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-07 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:113
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-08 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:114
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-09 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:115
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-10 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:116
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-11 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:117
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-12 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:118
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-13 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:148
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-14 · OPEN · _mapping mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:171
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_IsNot, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-15 · OPEN · _manifest mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:182
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-16 · OPEN · _manifest mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:185
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Is, ReplaceOrWithAnd, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-17 · OPEN · _manifest mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:192
- Mutants: 4 surviving at this location
- Mutation: `NumberReplacer, ReplaceComparisonOperator_NotEq_Gt, ReplaceUnaryOperator_Delete_USub, ReplaceUnaryOperator_USub_UAdd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-18 · OPEN · _manifest mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:199
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Is, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-19 · OPEN · _limit mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:209
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_Lt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-20 · OPEN · parse_explanation_request mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:220
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-21 · OPEN · parse_explanation_request mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:231
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-22 · OPEN · parse_explanation_request mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:238
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-23 · OPEN · parse_explanation_request mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:241
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-24 · OPEN · parse_explanation_request mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:250
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-25 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:275
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-26 · OPEN · elapsed_ms mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:310
- Mutants: 33 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-27 · OPEN · elapsed_ms mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:315
- Mutants: 4 surviving at this location
- Mutation: `NumberReplacer, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_FloorDiv_Div`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-28 · OPEN · stage_rows mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:320
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-29 · OPEN · stage_rows mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:330
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-30 · OPEN · _instant mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:360
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-31 · OPEN · _instant mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:365
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-32 · OPEN · _node_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:371
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-33 · OPEN · _node_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:372
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-34 · OPEN · _node_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:373
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-35 · OPEN · _node_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:377
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-36 · OPEN · _node_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:379
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-37 · OPEN · _node_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:382
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-38 · OPEN · _claim_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:389
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-39 · OPEN · _claim_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:390
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-40 · OPEN · _claim_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:391
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-41 · OPEN · _claim_stages mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:395
- Mutants: 4 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-42 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:413
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-43 · OPEN · graph_stage_evidence.add mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:423
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-44 · OPEN · graph_stage_evidence.add.text mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:427
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-45 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:450
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-46 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:453
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-47 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:456
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-48 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:466
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-49 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:470
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-50 · OPEN · graph_stage_evidence mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:472
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_GtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-51 · OPEN · node_failures mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:496
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-52 · OPEN · node_failures mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:497
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-53 · OPEN · node_failures mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:499
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-54 · OPEN · node_failures mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:500
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-55 · OPEN · require_document_bound mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:525
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-56 · OPEN · require_document_bound mutation survivor
- Track: mechanical
- Location: src/domain/operations/flow_explain.py:526
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Is`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
