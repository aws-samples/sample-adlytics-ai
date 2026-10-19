# Mutation review — src/adapters/inbound/conversation/server.py

- Commit: cea334aecd0c233863a10d61ad43741503ce3667
- Date: 2026-09-12
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/conversation/server.py TESTS='tests/unit/test_conversation_server.py'`
- Mutants: 144 generated, 59 killed, 85 survived
- Survivors: 85 open, 0 killed since, 0 equivalent

This Wave 0 run proves local test ownership only. Each survivor requires later semantic/equivalence triage before remediation.

## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:67
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:71
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:76
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-04 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:83
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-05 · OPEN · do_GET mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:98
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_LtE`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-06 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:130
- Mutants: 3 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, ReplaceOrWithAnd`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-07 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:147
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-08 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:150
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-09 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:153
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-10 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:167
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-11 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:193
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-12 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:196
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-13 · OPEN · _answer mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:199
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-14 · OPEN · _relay mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:229
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-15 · OPEN · _relay mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:233
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-16 · OPEN · _declined mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:246
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_Mul`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-17 · OPEN · _declined mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:258
- Mutants: 1 surviving at this location
- Mutation: `ExceptionReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-18 · OPEN · _declined mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:259
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-19 · OPEN · _declined mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:261
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-20 · OPEN · _preopen_error mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:276
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-21 · OPEN · _preopen_error mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:278
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-22 · OPEN · _preopen_error mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:279
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-23 · OPEN · _preopen_error mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:281
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-24 · OPEN · _begin_stream mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:288
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-25 · OPEN · _chunk mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:296
- Mutants: 33 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mod_RShift, ReplaceBinaryOperator_Add_BitAnd, ReplaceBinaryOperator_Add_RShift, ReplaceBinaryOperator_Add_LShift, ReplaceBinaryOperator_Add_BitAnd, ReplaceBinaryOperator_Add_Div, ReplaceBinaryOperator_Add_Mul, ReplaceBinaryOperator_Mod_Sub, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Mod_FloorDiv, ReplaceBinaryOperator_Mod_Pow, ReplaceBinaryOperator_Add_Div, ReplaceBinaryOperator_Add_LShift, ReplaceBinaryOperator_Add_Pow, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Mod_Add, ReplaceBinaryOperator_Mod_LShift, ReplaceBinaryOperator_Add_FloorDiv, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_Mod, ReplaceBinaryOperator_Add_Sub, ReplaceBinaryOperator_Add_Mod, ReplaceBinaryOperator_Mod_BitAnd, ReplaceBinaryOperator_Mod_Div, ReplaceBinaryOperator_Add_FloorDiv, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Mod_BitOr, ReplaceBinaryOperator_Mod_Mul, ReplaceBinaryOperator_Add_Pow, ReplaceBinaryOperator_Add_Mul, ReplaceBinaryOperator_Add_RShift, ReplaceBinaryOperator_Add_Sub, ReplaceBinaryOperator_Mod_BitXor`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-26 · OPEN · Server mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:325
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-27 · OPEN · serve mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:335
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN

## S-28 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/adapters/inbound/conversation/server.py:342
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_Is`
- Survives because: the local owner did not distinguish this mutation; semantic/equivalence triage is pending.
- Verdict: OPEN
