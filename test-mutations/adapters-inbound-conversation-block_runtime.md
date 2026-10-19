# Mutation review — src/adapters/inbound/conversation/block_runtime.py

- Commit: dcc9ef2c616512637733909738b323935e00b166
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/conversation/block_runtime.py TESTS='tests/unit/test_managed_conversation_runtime.py'`
- Mutants: 215 generated, 115 killed, 100 survived
- Survivors: 100 open, 0 killed since, 0 equivalent

This is a no-remediation sweep report. Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:80
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-02 · OPEN · _issue_reference mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:86
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:93
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:103
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:116
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:124
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-07 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:172
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-08 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:173
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-09 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:175
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-10 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:183
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-11 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:198
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-12 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:216
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_IsNot_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-13 · OPEN · run mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:228
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-14 · OPEN · _record_delivery mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:240
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-15 · OPEN · _assess_blocks mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:310
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-16 · OPEN · _assess_block mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:325
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-17 · OPEN · ManagedConversationRuntime mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:334
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-18 · OPEN · _published mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:336
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-19 · OPEN · _refused mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:348
- Mutants: 12 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_Mul_Div, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-20 · OPEN · _dependency_refused mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:361
- Mutants: 12 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_Mul_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Add`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-21 · OPEN · _dependency_refused mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:365
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-22 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:435
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-23 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:452
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Gt, AddNot, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_NotEq, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_GtE`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-24 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:459
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_IsNot_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-25 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:464
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Is, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-26 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:469
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Is, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-27 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:480
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-28 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:488
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_IsNot_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-29 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:492
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-30 · OPEN · _persist_audit mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:533
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-31 · OPEN · _terminal_log mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:572
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_Eq, AddNot, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq, ReplaceComparisonOperator_Is_IsNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-32 · OPEN · _operation_outcome mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:588
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_NotEq, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_Gt, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-33 · OPEN · _operation_outcome mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:593
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-34 · OPEN · _operation_outcome mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/block_runtime.py:594
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_NotEq, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_IsNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
