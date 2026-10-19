# Mutation review — src/adapters/config.py

- Commit: dcc9ef2c616512637733909738b323935e00b166
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/config.py TESTS='tests/unit/test_agent_quality_audit.py tests/unit/test_config.py tests/unit/test_presentation_handler.py tests/unit/test_producer_finalizer.py tests/unit/test_tracing_bootstrap.py'`
- Mutants: 303 generated, 106 killed, 197 survived
- Survivors: 197 open, 0 killed since, 0 equivalent

This is a no-remediation sweep report. Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:30
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:32
- Mutants: 18 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_LShift, ReplaceBinaryOperator_Mul_BitOr, ReplaceBinaryOperator_Mul_Add, ReplaceBinaryOperator_Mul_Pow, ReplaceBinaryOperator_Mul_Add, NumberReplacer, ReplaceBinaryOperator_Mul_BitXor, ReplaceBinaryOperator_Mul_BitOr, NumberReplacer, ReplaceBinaryOperator_Mul_Mod, ReplaceBinaryOperator_Mul_BitXor, NumberReplacer, ReplaceBinaryOperator_Mul_Pow, NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Mul_LShift, ReplaceBinaryOperator_Mul_Sub`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:36
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse, RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:43
- Mutants: 3 surviving at this location
- Mutation: `RemoveDecorator, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-05 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:52
- Mutants: 3 surviving at this location
- Mutation: `RemoveDecorator, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-06 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:62
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-07 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:70
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, RemoveDecorator, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-08 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:81
- Mutants: 3 surviving at this location
- Mutation: `RemoveDecorator, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-09 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:88
- Mutants: 3 surviving at this location
- Mutation: `ReplaceTrueWithFalse, RemoveDecorator, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-10 · OPEN · evaluation_operator_credentials mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:98
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_RShift`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-11 · OPEN · evaluation_operator_credentials mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:101
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_IsNot_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-12 · OPEN · evaluation_operator_credentials mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:106
- Mutants: 4 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot, ReplaceUnaryOperator_Delete_Not, ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-13 · OPEN · evaluation_operator_credentials mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:108
- Mutants: 3 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not, ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-14 · OPEN · evaluation_operator_credentials mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:110
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-15 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:123
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, RemoveDecorator, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-16 · OPEN · evidence_receipt_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:127
- Mutants: 3 surviving at this location
- Mutation: `AddNot, ReplaceOrWithAnd, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-17 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:136
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, RemoveDecorator, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-18 · OPEN · evaluation_writer_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:142
- Mutants: 6 surviving at this location
- Mutation: `ReplaceOrWithAnd, AddNot, ReplaceOrWithAnd, ReplaceUnaryOperator_Delete_Not, ReplaceUnaryOperator_Delete_Not, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-19 · OPEN · evaluation_writer_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:154
- Mutants: 1 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-20 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:161
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, RemoveDecorator, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-21 · OPEN · evaluation_outbox_dispatcher_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:170
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-22 · OPEN · evaluation_outbox_dispatcher_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:171
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-23 · OPEN · evaluation_outbox_dispatcher_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:172
- Mutants: 14 surviving at this location
- Mutation: `ReplaceOrWithAnd, ReplaceOrWithAnd, ReplaceComparisonOperator_Lt_Gt, NumberReplacer, ReplaceUnaryOperator_Delete_Not, ReplaceComparisonOperator_Lt_LtE, ReplaceComparisonOperator_Lt_GtE, ReplaceComparisonOperator_Lt_NotEq, AddNot, ReplaceOrWithAnd, ReplaceComparisonOperator_Lt_Eq, ReplaceUnaryOperator_Delete_Not, NumberReplacer, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-24 · OPEN · evaluation_outbox_dispatcher_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:183
- Mutants: 7 surviving at this location
- Mutation: `NumberReplacer, ReplaceComparisonOperator_Gt_GtE, NumberReplacer, ReplaceComparisonOperator_Gt_Lt, ReplaceComparisonOperator_Gt_NotEq, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_LtE`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-25 · OPEN · evaluation_outbox_dispatcher_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:185
- Mutants: 1 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-26 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:197
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-27 · OPEN · agent_quality_processor_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:202
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-28 · OPEN · agent_quality_processor_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:213
- Mutants: 1 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-29 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:220
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-30 · OPEN · evaluation_result_ingress_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:232
- Mutants: 2 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-31 · OPEN · evaluation_result_ingress_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:248
- Mutants: 1 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-32 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:261
- Mutants: 3 surviving at this location
- Mutation: `RemoveDecorator, NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-33 · OPEN · evaluation_result_persister_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:265
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-34 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:284
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-35 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:285
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-36 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:286
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-37 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:287
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-38 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:290
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-39 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:295
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-40 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:296
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-41 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:307
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-42 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:308
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-43 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:309
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-44 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:310
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-45 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:311
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-46 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:312
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-47 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:313
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-48 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:314
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-49 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:315
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-50 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:323
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-51 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:326
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-52 · OPEN · _must_be_a_deployed_https_origin mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:330
- Mutants: 4 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-53 · OPEN · _must_be_a_deployed_https_origin mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:331
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-54 · OPEN · Settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:336
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-55 · OPEN · load_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:392
- Mutants: 12 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_Pow`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-56 · OPEN · _with_resolved_secret mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:414
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-57 · OPEN · _with_resolved_secret mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:427
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-58 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:460
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, RemoveDecorator, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-59 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:466
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-60 · OPEN · read_tracing_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:481
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Add`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-61 · OPEN · read_tracing_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:488
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-62 · OPEN · read_tracing_settings mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:489
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_LtE_Lt`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-63 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:500
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-64 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/config.py:506
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
