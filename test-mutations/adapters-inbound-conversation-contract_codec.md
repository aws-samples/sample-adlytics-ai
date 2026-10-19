# Mutation review — src/adapters/inbound/conversation/contract_codec.py

- Commit: dcc9ef2c616512637733909738b323935e00b166
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/conversation/contract_codec.py TESTS='tests/unit/test_agentcore_harness.py tests/unit/test_capability_public_projection.py tests/unit/test_conversation_publication_pipeline.py tests/unit/test_initial_plan_assessment_transport.py tests/unit/test_presentation_finalizer.py tests/unit/test_structured_conversation_contracts.py'`
- Mutants: 314 generated, 216 killed, 98 survived
- Survivors: 98 open, 0 killed since, 0 equivalent

This is a no-remediation sweep report. Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:33
- Mutants: 15 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div, ReplaceBinaryOperator_Mul_Sub, ReplaceBinaryOperator_Mul_LShift, NumberReplacer, ReplaceBinaryOperator_Mul_BitOr, ReplaceBinaryOperator_Mul_Pow, NumberReplacer, ReplaceBinaryOperator_Mul_RShift, ReplaceBinaryOperator_Mul_BitAnd, ReplaceBinaryOperator_Mul_Add, ReplaceBinaryOperator_Mul_Mod, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Mul_FloorDiv, ReplaceBinaryOperator_Mul_BitXor`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-02 · OPEN · _json_value mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:62
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-03 · OPEN · _json_value mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:63
- Mutants: 2 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-04 · OPEN · _json_value mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:66
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-05 · OPEN · _json_value mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:68
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-06 · OPEN · _json_value mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:70
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-07 · OPEN · _json_value mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:71
- Mutants: 4 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot, ReplaceUnaryOperator_Delete_Not, ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-08 · OPEN · _decode_union mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:84
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-09 · OPEN · _decode_union mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:85
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-10 · OPEN · _decode_union mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:88
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Lt`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-11 · OPEN · _decode_union mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:90
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-12 · OPEN · _decode_dataclass mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:104
- Mutants: 6 surviving at this location
- Mutation: `ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_BitAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-13 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:124
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-14 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:127
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_NotEq, ReplaceComparisonOperator_Eq_Lt`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-15 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:129
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-16 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:132
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-17 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:136
- Mutants: 5 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_NotEq, ReplaceComparisonOperator_NotEq_IsNot, ReplaceOrWithAnd, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-18 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:141
- Mutants: 2 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-19 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:144
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_NotEq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-20 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:150
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-21 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:152
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-22 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:156
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-23 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:157
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-24 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:161
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-25 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:163
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-26 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:164
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-27 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:168
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-28 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:170
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-29 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:174
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-30 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:175
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-31 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:178
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-32 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:179
- Mutants: 3 surviving at this location
- Mutation: `ReplaceOrWithAnd, AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-33 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:182
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-34 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:183
- Mutants: 2 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not, AddNot`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-35 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:187
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-36 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:189
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-37 · OPEN · _decode mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:199
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-38 · OPEN · _materialize mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:214
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-39 · OPEN · _canonical_json mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:231
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-40 · OPEN · _canonical_json mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:234
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-41 · OPEN · _validate_public_stream_bound mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:243
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Is`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-42 · OPEN · _validate_public_stream_bound mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:244
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_NotEq, ReplaceComparisonOperator_Gt_GtE, ReplaceAndWithOr, ReplaceComparisonOperator_Gt_Is, ReplaceComparisonOperator_Gt_IsNot, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_LtE, ReplaceComparisonOperator_Gt_Lt`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-43 · OPEN · _structured_transport mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:269
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-44 · OPEN · _structured_payload mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:275
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-45 · OPEN · _response_draft_payload mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:299
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Gt`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-46 · OPEN · _response_draft_payload mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:302
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN

## S-47 · OPEN · decode_presentation_plan mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/contract_codec.py:355
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the sweep records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
