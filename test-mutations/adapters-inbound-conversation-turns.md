# Mutation review — src/adapters/inbound/conversation/turns.py

- Commit: dcc9ef2c616512637733909738b323935e00b166
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/conversation/turns.py TESTS='tests/unit/test_managed_conversation_runtime.py tests/unit/test_managed_conversation_turns.py'`
- Mutants: 72 generated, 12 killed, 60 survived
- Survivors: 60 open, 0 killed since, 0 equivalent

This is a no-remediation sweep report. Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:31
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:47
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:48
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:49
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:50
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:55
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, RemoveDecorator, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-07 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:64
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_LtE, AddNot, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-08 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:66
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-09 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:68
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_LtE, ReplaceComparisonOperator_Gt_NotEq, AddNot, ReplaceComparisonOperator_Gt_IsNot, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_Lt, ReplaceComparisonOperator_Gt_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-10 · OPEN · __post_init__ mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:70
- Mutants: 10 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_IsNot, ReplaceComparisonOperator_Gt_Lt, ReplaceOrWithAnd, ReplaceComparisonOperator_Gt_NotEq, ReplaceUnaryOperator_Delete_Not, AddNot, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_Is, ReplaceComparisonOperator_Gt_LtE`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-11 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:74
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, RemoveDecorator, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-12 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:85
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-13 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:96
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse, RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-14 · OPEN · ConversationTooling mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:103
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-15 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:110
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-16 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:115
- Mutants: 1 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-17 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:116
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-18 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:124
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-19 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:145
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-20 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:151
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-21 · OPEN · of mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:153
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-22 · OPEN · turn_from mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:161
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-23 · OPEN · _conversation_request mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:199
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-24 · OPEN · _conversation_request mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/turns.py:206
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
