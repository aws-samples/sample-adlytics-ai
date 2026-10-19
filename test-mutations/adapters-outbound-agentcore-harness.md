# Mutation review — src/adapters/outbound/agentcore/harness.py

- Commit: 6b8dce224782dc9eaa0d3d35074c8c13acc65ecf
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/outbound/agentcore/harness.py TESTS='tests/unit/test_agentcore_harness.py'`
- Mutants: 780 generated, 526 killed, 254 survived before semantic triage
- Survivors: 209 open test gaps, 7 killed since prior run, 45 equivalent

This retained re-mutation follows Wave 1 adversarial test remediation. Each remaining survivor requires semantic/equivalence triage before further test changes.

## S-01 · EQUIVALENT · TYPE_CHECKING import guard
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:46
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: with postponed annotations, importing the Bedrock SDK type aliases at runtime changes no Harness protocol, custody, result, or transport behavior.
- Verdict: EQUIVALENT

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:57
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:58
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:59
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:60
- Mutants: 6 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Mul_LShift, ReplaceBinaryOperator_Mul_Pow`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:61
- Mutants: 9 surviving at this location
- Mutation: `NumberReplacer, ReplaceBinaryOperator_Mul_Add, ReplaceBinaryOperator_Mul_Pow, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Mul_LShift, ReplaceBinaryOperator_Mul_BitOr, ReplaceBinaryOperator_Mul_BitXor, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-07 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:77
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-08 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:86
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-09 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:91
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_LtE, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-10 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:96
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-11 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:103
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-12 · OPEN · _Normalizer mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:118
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-13 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:143
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-14 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:145
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-15 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:149
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-16 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:151
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-17 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:152
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-18 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:155
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-19 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:162
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-20 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:164
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-21 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:168
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-22 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:172
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-23 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:178
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-24 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:181
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, AddNot, ReplaceComparisonOperator_Eq_NotEq, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-25 · OPEN · accept mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:182
- Mutants: 2 surviving at this location
- Mutation: `ReplaceOrWithAnd, AddNot`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-26 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:192
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-27 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:193
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-28 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:194
- Mutants: 4 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-29 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:202
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-30 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:206
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-31 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:211
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-32 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:214
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-33 · OPEN · _stop mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:222
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-34 · OPEN · _message_is mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:233
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-35 · OPEN · _start mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:254
- Mutants: 11 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, AddNot, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_NotEq`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-36 · OPEN · _start mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:269
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-37 · OPEN · _start mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:270
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-38 · OPEN · _start mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:275
- Mutants: 6 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_NotEq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-39 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:287
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-40 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:292
- Mutants: 4 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-41 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:293
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-42 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:294
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_IsNot`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-43 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:301
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-44 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:302
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-45 · OPEN · _delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:303
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-46 · OPEN · _assistant_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:309
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-47 · OPEN · _assistant_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:311
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-48 · OPEN · _assistant_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:317
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_Is, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-49 · OPEN · _assistant_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:321
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_NotEq, AddNot, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-50 · OPEN · _assistant_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:323
- Mutants: 9 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_NotEq, ReplaceComparisonOperator_LtE_Lt, ReplaceComparisonOperator_LtE_Gt, ReplaceAndWithOr, ReplaceComparisonOperator_LtE_Eq, AddNot, ReplaceComparisonOperator_LtE_IsNot, ReplaceComparisonOperator_LtE_Is, ReplaceComparisonOperator_LtE_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-51 · OPEN · _tool_use_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:335
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-52 · OPEN · _tool_use_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:336
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-53 · OPEN · _tool_use_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:337
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-54 · OPEN · _tool_use_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:340
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-55 · OPEN · _tool_result_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:346
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Gt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-56 · OPEN · _tool_result_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:350
- Mutants: 2 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-57 · OPEN · _tool_result_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:354
- Mutants: 5 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-58 · OPEN · _tool_result_delta mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:361
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-59 · OPEN · take_pre_tool_update mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:368
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_LShift`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-60 · OPEN · take_pre_tool_update mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:371
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-61 · OPEN · finish mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:375
- Mutants: 2 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-62 · OPEN · finish mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:377
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-63 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:422
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-64 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:424
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-65 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:426
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-66 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:431
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-67 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:433
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-68 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:435
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-69 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:438
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-70 · OPEN · _finalizer_accepted mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:444
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-71 · OPEN · _result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:448
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_RShift`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-72 · OPEN · _result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:452
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_LtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-73 · OPEN · _result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:456
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-74 · OPEN · _result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:465
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-75 · OPEN · _result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:466
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-76 · OPEN · _serialized_result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:493
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-77 · OPEN · _serialized_result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:498
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-78 · OPEN · _serialized_result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:501
- Mutants: 2 surviving at this location
- Mutation: `ReplaceFalseWithTrue, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-79 · OPEN · _serialized_result mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:503
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_Is, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Eq`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-80 · OPEN · _decoded_refusal_grounding mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:515
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-81 · OPEN · _decoded_grounding mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:521
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-82 · OPEN · _decoded_grounding mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:527
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-83 · OPEN · _decoded_grounding mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:529
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-84 · OPEN · _decoded_grounding mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:531
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-85 · OPEN · _decoded_grounding mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:539
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-86 · OPEN · _decode_finalizer_input mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:545
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_Is`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-87 · OPEN · _decode_finalizer_input mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:558
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-88 · OPEN · _decode_finalizer_input mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:561
- Mutants: 4 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-89 · OPEN · _logical_tool_name mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:568
- Mutants: 2 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-90 · OPEN · _optional_object mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:579
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_RShift`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-91 · OPEN · _text mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:584
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-92 · OPEN · _index mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:591
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_NotEq, ReplaceOrWithAnd, NumberReplacer, ReplaceComparisonOperator_Lt_Gt, ReplaceOrWithAnd`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-93 · OPEN · _completed_tool_use_message mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:608
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-94 · OPEN · _completed_tool_use_message mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:610
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-95 · OPEN · _completed_tool_result_message mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:620
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_NotEq, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_IsNot`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-96 · OPEN · _completed_tool_result_message mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:622
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Eq`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-97 · OPEN · __init__ mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:633
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Add`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-98 · OPEN · __init__ mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:641
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN

## S-99 · OPEN · stream mutation survivor
- Track: mechanical
- Location: src/adapters/outbound/agentcore/harness.py:648
- Mutants: 6 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_NotEq, ReplaceComparisonOperator_LtE_Is, ReplaceComparisonOperator_LtE_GtE, ReplaceComparisonOperator_LtE_Lt, ReplaceComparisonOperator_LtE_IsNot, ReplaceComparisonOperator_LtE_Eq`
- Survives because: behavior/equivalence triage remains pending after the Wave 1 adversarial cases.
- Verdict: OPEN


## Retained survivor triage

The generated entries above are the raw Cosmic Ray inventory. The dispositions below are authoritative for the current retained run.

### Killed since the prior Harness run

The Wave 1 adversarial tests increased kills from 466 to 481. The confirmed source defect was `ManagedHarnessTarget.read_timeout_seconds`: a positive float was accepted despite the target contract requiring a positive integer. `ManagedHarnessTarget.__post_init__` now rejects non-integers, and `test_managed_harness_target_requires_a_positive_integer_timeout` covers booleans, zero, negatives, and a positive float.

### Equivalent survivors

| Raw entry | Location | Mutants | Classification | Reason |
|---|---|---:|---|---|
| S-01 | `harness.py:46` | 1 | EQUIVALENT | Negating the `TYPE_CHECKING` import guard changes no executable Harness protocol behavior under postponed annotations. |
| S-59 | `harness.py:368` | 11 | EQUIVALENT | Replacements of the optional return-type union are postponed annotations and are never evaluated by `take_pre_tool_update`. |
| S-71 | `harness.py:448` | 11 | EQUIVALENT | Replacements of `_result`'s optional return-type union do not change result decoding. |
| S-90 | `harness.py:579` | 11 | EQUIVALENT | Replacements of `_optional_object`'s optional return-type union are annotation-only. |
| S-97 | `harness.py:633` | 11 | EQUIVALENT | Replacements of the optional SDK-client constructor annotation do not change client construction. |

### Open survivors

Every raw survivor other than S-01, S-59, S-71, S-90, and S-97 remains an **OPEN test gap**. No additional source defect was established by this triage. The gaps group into these executable invariants:

- lifecycle admission and terminal state: `accept`, `_stop`, `_message_is`, `finish`, and the activity predicates;
- content-block framing: `_start`, `_delta`, `_assistant_delta`, `_tool_use_delta`, and `_tool_result_delta`;
- finalizer attestation and plan/result custody: `_finalizer_accepted`, `_result`, `_serialized_result`, `_decoded_refusal_grounding`, `_decoded_grounding`, and `_decode_finalizer_input`;
- wire scalar validation: `_logical_tool_name`, `_optional_object`, `_text`, `_index`, target construction, and session bounds;
- structural dataclass/decorator mutations at module-level declarations, which require a later explicit API-immutability decision rather than incidental happy-path assertions.

The next remediation slice should select one invariant family at a time, add adversarial tests for that family, retain a module-only mutation database, and mark only the killed/equivalent raw entries in this report. Do not add tests merely to reduce the aggregate count.


### Lifecycle-admission re-mutation delta

The lifecycle-admission adversarial suite in commit `c85cc07` changed the retained Harness outcome from 481 killed / 299 raw survivors to 501 killed / 279 raw survivors. The entries below remain OPEN because their line still has surviving mutations; the table records the exact killed subset.

| Raw entry | Location | Before | After | Killed by lifecycle tests |
|---|---|---:|---:|---:|
| S-31 | `_stop` at `harness.py:211` | 3 | 1 | 2 |
| S-32 | `_stop` at `harness.py:214` | 5 | 3 | 2 |
| S-34 | `_message_is` at `harness.py:233` | 7 | 3 | 4 |
| S-35 | `_start` at `harness.py:254` | 11 | 2 | 9 |
| S-61 | `finish` at `harness.py:375` | 2 | 1 | 1 |
| S-92 | `_index` at `harness.py:591` | 5 | 3 | 2 |

The next Harness family should address content-block framing and result decoding, not repeat these lifecycle cases.


### Content-framing and decoder re-mutation delta

The content-framing suite in commit `bef3ad1` changed the retained Harness outcome from 501 killed / 279 raw survivors to 519 killed / 261 raw survivors. The tests killed 18 mutations across provider event cardinality, delta framing, assistant role, finalizer input, tool/result representation, scalar validation, and session bounds.

| Location | Before | After | Killed |
|---|---:|---:|---:|
| module constants at `harness.py:58-59` | 3 | 1 | 2 |
| `_delta` at `harness.py:292-294` | 9 | 4 | 5 |
| `_assistant_delta` at `harness.py:309` | 3 | 2 | 1 |
| `_tool_use_delta` at `harness.py:337` | 1 | 0 | 1 |
| `_tool_result_delta` at `harness.py:346,350` | 5 | 1 | 4 |
| `_decode_finalizer_input` at `harness.py:558` | 1 | 0 | 1 |
| `_index` at `harness.py:591` | 3 | 2 | 1 |
| `stream` session bounds at `harness.py:648` | 6 | 3 | 3 |

The next remaining Harness family is finalizer attestation and exact success/refusal envelope decoding; it should be completed before a new broad Harness score is interpreted.


### Finalizer-attestation and custody re-mutation delta

The attestation suite in commit `0047882` changed the retained Harness outcome from 519 killed / 261 raw survivors to 526 killed / 254 raw survivors.

| Location | Before | After | Killed |
|---|---:|---:|---:|
| `_finalizer_accepted` at `harness.py:422` | 3 | 1 | 2 |
| `_finalizer_accepted` at `harness.py:426` | 3 | 1 | 2 |
| `_finalizer_accepted` at `harness.py:438` | 1 | 0 | 1 |
| `_result` at `harness.py:466` | 3 | 1 | 2 |

The remaining open Harness survivors are now explicitly limited to semantic/equivalence review of state-machine comparisons, structural declarations, and lower-level decoder scalars; all currently identified public lifecycle, content framing, finalizer-attestation, and exact envelope families have a retained proof cycle.
