# Mutation review — src/domain/operations/execution_graph_dispatch.py
- Commit: ced56e83ef455ec44c1812c06447da0cd2e88183 (S-48 to S-71 re-review); full sweep 4c3c73153ff93ad11fcb6db8469d6e5e7f30f477
- Date: 2026-10-15
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/operations/execution_graph_dispatch.py TESTS='tests/unit/test_execution_graph_dispatch.py'` (full sweep); re-review: the same cosmic-ray configuration with `test-command = "python -m pytest tests/unit/test_execution_graph_dispatch.py tests/unit/test_execution_graph_unit_batching.py tests/unit/test_execution_graph_release_store.py tests/unit/test_execution_graph_dispatch_store.py tests/unit/test_graph_publish.py tests/unit/test_execution_graph_promoter.py -q -x"`, the session pruned to mutation_specs rows 617-766 before `cosmic-ray exec`
- Mutants: 780 generated, 319 killed, 461 survived (full sweep); re-review session 237 generated, 204 killed, 33 survived
- Survivors: 334 open, 96 killed since, 31 equivalent
This is a no-remediation report taken at the rolling-freshness convergence tip (plan item 9). Survivor locations and operators are captured here; manual semantic triage has not yet classified equivalent mutants or selected remediation.
Re-review at ced56e83 (implementation-review MED-2/MED-3): S-48 through S-71 were triaged on a clean tree against the six test files that exercise the module. Of their 127 recorded mutants, 96 are now killed by named tests and 31 are equivalent. S-47 (two dataclass-flag survivors on line 618) is outside that scope and stays OPEN. Every other entry is unchanged from the full sweep.
## S-01 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:209
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-02 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:230
- Mutants: 26 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_FloorDiv_Add, ReplaceBinaryOperator_FloorDiv_BitAnd, ReplaceBinaryOperator_FloorDiv_BitOr, ReplaceBinaryOperator_FloorDiv_BitXor, ReplaceBinaryOperator_FloorDiv_Div, ReplaceBinaryOperator_FloorDiv_LShift, ReplaceBinaryOperator_FloorDiv_Mod, ReplaceBinaryOperator_FloorDiv_Mul, ReplaceBinaryOperator_FloorDiv_Pow, ReplaceBinaryOperator_FloorDiv_RShift, ReplaceBinaryOperator_FloorDiv_Sub, ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_Div, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mod, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow, ReplaceBinaryOperator_Sub_RShift`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-03 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:233
- Mutants: 26 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_FloorDiv_Add, ReplaceBinaryOperator_FloorDiv_BitAnd, ReplaceBinaryOperator_FloorDiv_BitOr, ReplaceBinaryOperator_FloorDiv_BitXor, ReplaceBinaryOperator_FloorDiv_Div, ReplaceBinaryOperator_FloorDiv_LShift, ReplaceBinaryOperator_FloorDiv_Mod, ReplaceBinaryOperator_FloorDiv_Mul, ReplaceBinaryOperator_FloorDiv_Pow, ReplaceBinaryOperator_FloorDiv_RShift, ReplaceBinaryOperator_FloorDiv_Sub, ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_Div, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mod, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow, ReplaceBinaryOperator_Sub_RShift`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-04 · OPEN · is_write_unit mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:245
- Mutants: 9 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceAndWithOr, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-05 · OPEN · unit_final_attempt mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:258
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-06 · OPEN · unit_final_attempt mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:265
- Mutants: 8 surviving at this location
- Mutation: `ReplaceComparisonOperator_GtE_Eq, ReplaceComparisonOperator_GtE_Gt, ReplaceComparisonOperator_GtE_Is, ReplaceComparisonOperator_GtE_IsNot, ReplaceComparisonOperator_GtE_Lt, ReplaceComparisonOperator_GtE_LtE, ReplaceComparisonOperator_GtE_NotEq, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-07 · OPEN · unit_may_return mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:270
- Mutants: 1 surviving at this location
- Mutation: `ReplaceUnaryOperator_Delete_Not`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-08 · OPEN · sets_final_return mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:296
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-09 · OPEN · GrantCandidate mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:302
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-10 · OPEN · GrantCandidate.root_clock_started_at mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:303
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-11 · OPEN · GrantCandidate mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:307
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-12 · OPEN · GrantCandidate mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:312
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-13 · OPEN · grant_order_key mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:320
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-14 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:324
- Mutants: 12 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub, ReplaceBinaryOperator_Mul_Div`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-15 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:333
- Mutants: 8 surviving at this location
- Mutation: `AddNot, NumberReplacer, NumberReplacer, ReplaceComparisonOperator_Lt_Eq, ReplaceComparisonOperator_Lt_Gt, ReplaceComparisonOperator_Lt_GtE, ReplaceComparisonOperator_Lt_LtE, ReplaceComparisonOperator_Lt_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-16 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:336
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-17 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:338
- Mutants: 3 surviving at this location
- Mutation: `AddNot, NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-18 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:339
- Mutants: 2 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-19 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:341
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-20 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:342
- Mutants: 9 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_NotEq, ReplaceOrWithAnd`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-21 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:343
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBreakWithContinue`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-22 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:344
- Mutants: 5 surviving at this location
- Mutation: `AddNot, ReplaceAndWithOr, ReplaceAndWithOr, ReplaceComparisonOperator_IsNot_Is, ReplaceUnaryOperator_Delete_Not`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-23 · OPEN · take_grant mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:345
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBreakWithContinue`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-24 · OPEN · ArmableNode mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:477
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-25 · OPEN · ArmableNode mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:482
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-26 · OPEN · ArmableNode mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:487
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-27 · OPEN · ArmableNode mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:492
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-28 · OPEN · template_node mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:500
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-29 · OPEN · node_route mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:503
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-30 · OPEN · _predecessor_ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:514
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-31 · OPEN · _predecessor_ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:515
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-32 · OPEN · _predecessor_ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:517
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-33 · OPEN · _predecessor_ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:518
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-34 · OPEN · _predecessor_ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:519
- Mutants: 8 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-35 · OPEN · scope_pending mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:531
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-36 · OPEN · scope_pending mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:532
- Mutants: 8 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-37 · OPEN · scope_pending mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:533
- Mutants: 2 surviving at this location
- Mutation: `ReplaceAndWithOr, ReplaceComparisonOperator_IsNot_Is`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-38 · OPEN · arm_eligible.ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:548
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_Lt, ReplaceComparisonOperator_Eq_LtE, ReplaceComparisonOperator_Eq_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-39 · OPEN · arm_eligible.ready mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:549
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-40 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:558
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-41 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:570
- Mutants: 4 surviving at this location
- Mutation: `ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Add_BitXor`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-42 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:571
- Mutants: 4 surviving at this location
- Mutation: `ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitOr, ReplaceBinaryOperator_Add_BitXor, ReplaceBinaryOperator_Add_BitXor`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-43 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:574
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-44 · OPEN · ServingAuthorityIdentity.__post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:586
- Mutants: 4 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, ReplaceComparisonOperator_Lt_Eq, ReplaceComparisonOperator_Lt_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-45 · OPEN · require_serving_authority_identity mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:594
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_IsNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-46 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:598
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-47 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:618
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-48 · EQUIVALENT · ServingFrontierVector.__post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:628
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt`
- Killed since: `NotEq_Gt, NotEq_Lt` by `tests/unit/test_execution_graph_dispatch.py::test_a_frontier_vector_refuses_any_shard_count_but_the_protocol_32`
- Equivalent: `NotEq_IsNot` — small-int identity: an equal length here is always the protocol 32, which CPython caches
- Verdict: EQUIVALENT
## S-49 · KILLED · ServingFrontierVector.__post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:633
- Mutants: 1 surviving at this location
- Mutation: `NumberReplacer`
- Killed since: `NumberReplacer` by `tests/unit/test_execution_graph_dispatch.py::test_a_frontier_vector_refuses_a_negative_revision_or_count`
- Verdict: KILLED
## S-50 · KILLED · ServingFrontierVector.__post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:639
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_IsNot, ReplaceComparisonOperator_Gt_Lt, ReplaceComparisonOperator_Gt_NotEq`
- Killed since: `Gt_IsNot, Gt_Lt, Gt_NotEq` by `tests/unit/test_execution_graph_dispatch.py::test_a_frontier_vector_accepts_eligible_below_visible_and_refuses_eligible_above`
- Verdict: KILLED
## S-51 · EQUIVALENT · ServingFrontierVector.__post_init__ mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:640
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Equivalent: `ReplaceTrueWithFalse` — `zip(strict=...)` cannot matter: the shard-length guard on line 627 already proves equal lengths
- Verdict: EQUIVALENT
## S-52 · KILLED · ServingFrontierVector mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:644
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Killed since: `RemoveDecorator` by `tests/unit/test_execution_graph_dispatch.py::test_a_frontier_vector_accepts_eligible_below_visible_and_refuses_eligible_above`
- Verdict: KILLED
## S-53 · KILLED · ServingFrontierVector mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:649
- Mutants: 1 surviving at this location
- Mutation: `RemoveDecorator`
- Killed since: `RemoveDecorator` by `tests/unit/test_execution_graph_dispatch.py::test_a_frontier_vector_accepts_eligible_below_visible_and_refuses_eligible_above`
- Verdict: KILLED
## S-54 · KILLED · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:671
- Mutants: 1 surviving at this location
- Mutation: `ReplaceComparisonOperator_Gt_GtE`
- Killed since: `Gt_GtE` by `tests/unit/test_execution_graph_dispatch.py::test_a_full_512_candidate_roster_is_accepted_at_its_bound`
- Verdict: KILLED
## S-55 · EQUIVALENT · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:676
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt`
- Killed since: `NotEq_Lt` by `tests/unit/test_execution_graph_dispatch.py::test_a_roster_spanning_two_domains_is_refused`
- Equivalent: `NotEq_Gt` — a nonempty roster has at least one domain, so `!= 1` equals `> 1`
- Verdict: EQUIVALENT
## S-56 · KILLED · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:689
- Mutants: 4 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, ReplaceComparisonOperator_Lt_Eq, ReplaceComparisonOperator_Lt_LtE`
- Killed since: `NumberReplacer, NumberReplacer, Lt_Eq, Lt_LtE` by `tests/unit/test_execution_graph_dispatch.py::test_ordinal_zero_is_eligible_and_a_negative_ordinal_is_refused`
- Verdict: KILLED
## S-57 · EQUIVALENT · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:692
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_NotEq`
- Killed since: `IsNot_Gt, IsNot_Lt` by `tests/unit/test_execution_graph_dispatch.py::test_every_non_granted_candidate_run_state_is_refused`
- Equivalent: `IsNot_NotEq` — enum identity: RunState/NodeState members are unique singletons, so `!=` equals `is not`
- Verdict: EQUIVALENT
## S-58 · KILLED · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:694
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Killed since: `ReplaceAndWithOr` by `tests/unit/test_execution_graph_dispatch.py::test_a_candidate_missing_either_arm_or_complete_authority_is_refused`
- Verdict: KILLED
## S-59 · EQUIVALENT · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:696
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_NotEq`
- Killed since: `IsNot_Gt, IsNot_Lt` by `tests/unit/test_execution_graph_dispatch.py::test_every_unsettled_candidate_publication_state_is_refused`
- Equivalent: `IsNot_NotEq` — enum identity: RunState/NodeState members are unique singletons, so `!=` equals `is not`
- Verdict: EQUIVALENT
## S-60 · EQUIVALENT · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:699
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_Lt, ReplaceComparisonOperator_IsNot_NotEq`
- Killed since: `IsNot_Gt` by `tests/unit/test_execution_graph_dispatch.py::test_every_started_candidate_worker_node_state_is_refused`
- Equivalent: `IsNot_Lt, IsNot_NotEq` — enum identity: RunState/NodeState members are unique singletons, so `!=` equals `is not`; WAITING_INPUT is the largest NodeState value, so `<` means not equal
- Verdict: EQUIVALENT
## S-61 · EQUIVALENT · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:700
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_IsNot_Gt, ReplaceComparisonOperator_IsNot_NotEq`
- Killed since: `IsNot_Gt` by `tests/unit/test_execution_graph_dispatch.py::test_every_started_candidate_worker_node_state_is_refused`
- Equivalent: `IsNot_NotEq` — enum identity: RunState/NodeState members are unique singletons, so `!=` equals `is not`
- Verdict: EQUIVALENT
## S-62 · KILLED · validate_serving_candidates mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:703
- Mutants: 1 surviving at this location
- Mutation: `ReplaceOrWithAnd`
- Killed since: `ReplaceOrWithAnd` by `tests/unit/test_execution_graph_dispatch.py::test_a_candidate_holding_either_runtime_or_binding_authority_is_refused`
- Verdict: KILLED
## S-63 · EQUIVALENT · serving_gap mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:714
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Equivalent: `all 11 BitOr substitutions` — type annotation under `from __future__ import annotations` (never evaluated)
- Verdict: EQUIVALENT
## S-64 · KILLED · serving_gap mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:720
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE`
- Killed since: `Eq_GtE, Eq_Is, Eq_LtE` by `tests/unit/test_execution_graph_dispatch.py::test_only_same_domain_lower_obligations_are_gaps`
- Verdict: KILLED
## S-65 · EQUIVALENT · serving_gap mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:722
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt`
- Killed since: `NotEq_Lt, NotEq_IsNot` by `tests/unit/test_execution_graph_dispatch.py::test_duplicate_open_obligation_ordinals_are_fatal`, `tests/unit/test_execution_graph_dispatch.py::test_more_than_256_unique_open_obligations_compare_counts_by_value`
- Equivalent: `NotEq_Gt` — a list is never shorter than its set, so `!=` equals `>`
- Verdict: EQUIVALENT
## S-66 · KILLED · serving_gap mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:731
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Lt_IsNot, ReplaceComparisonOperator_Lt_LtE, ReplaceComparisonOperator_Lt_NotEq`
- Killed since: `Lt_IsNot, Lt_LtE, Lt_NotEq` by `tests/unit/test_execution_graph_dispatch.py::test_obligations_at_or_above_the_winner_ordinal_are_never_gaps`
- Verdict: KILLED
## S-67 · EQUIVALENT · newest_eligible_candidate mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:747
- Mutants: 11 surviving at this location
- Mutation: `ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub`
- Equivalent: `all 11 BitOr substitutions` — type annotation under `from __future__ import annotations` (never evaluated)
- Verdict: EQUIVALENT
## S-68 · KILLED · serving_frontier_shard mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:760
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Killed since: `AddNot, ReplaceUnaryOperator_Delete_Not` by `tests/unit/test_execution_graph_dispatch.py::test_the_frontier_shard_mapping_is_pinned_for_fixed_run_ids`
- Verdict: KILLED
## S-69 · KILLED · serving_frontier_shard mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:762
- Mutants: 25 surviving at this location
- Mutation: `AddNot, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_BitAnd_Add, ReplaceBinaryOperator_BitAnd_BitOr, ReplaceBinaryOperator_BitAnd_BitXor, ReplaceBinaryOperator_BitAnd_Div, ReplaceBinaryOperator_BitAnd_FloorDiv, ReplaceBinaryOperator_BitAnd_LShift, ReplaceBinaryOperator_BitAnd_Mod, ReplaceBinaryOperator_BitAnd_Mul, ReplaceBinaryOperator_BitAnd_Pow, ReplaceBinaryOperator_BitAnd_RShift, ReplaceBinaryOperator_BitAnd_Sub, ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_Div, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mod, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow, ReplaceBinaryOperator_Sub_RShift`
- Killed since: `all 25` by `tests/unit/test_execution_graph_dispatch.py::test_the_frontier_shard_mapping_is_pinned_for_fixed_run_ids`, `tests/unit/test_execution_graph_dispatch.py::test_a_non_power_of_two_shard_count_is_refused`, `tests/unit/test_execution_graph_dispatch.py::test_other_power_of_two_shard_counts_use_their_own_leading_bits`
- Verdict: KILLED
## S-70 · KILLED · serving_frontier_shard mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:764
- Mutants: 13 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_Div, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mod, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow, ReplaceBinaryOperator_Sub_RShift`
- Killed since: `all 13` by `tests/unit/test_execution_graph_dispatch.py::test_the_frontier_shard_mapping_is_pinned_for_fixed_run_ids`
- Verdict: KILLED
## S-71 · KILLED · serving_frontier_shard mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:766
- Mutants: 26 surviving at this location
- Mutation: `NumberReplacer, NumberReplacer, NumberReplacer, NumberReplacer, ReplaceBinaryOperator_RShift_Add, ReplaceBinaryOperator_RShift_BitAnd, ReplaceBinaryOperator_RShift_BitOr, ReplaceBinaryOperator_RShift_BitXor, ReplaceBinaryOperator_RShift_Div, ReplaceBinaryOperator_RShift_FloorDiv, ReplaceBinaryOperator_RShift_LShift, ReplaceBinaryOperator_RShift_Mod, ReplaceBinaryOperator_RShift_Mul, ReplaceBinaryOperator_RShift_Pow, ReplaceBinaryOperator_RShift_Sub, ReplaceBinaryOperator_Sub_Add, ReplaceBinaryOperator_Sub_BitAnd, ReplaceBinaryOperator_Sub_BitOr, ReplaceBinaryOperator_Sub_BitXor, ReplaceBinaryOperator_Sub_Div, ReplaceBinaryOperator_Sub_FloorDiv, ReplaceBinaryOperator_Sub_LShift, ReplaceBinaryOperator_Sub_Mod, ReplaceBinaryOperator_Sub_Mul, ReplaceBinaryOperator_Sub_Pow, ReplaceBinaryOperator_Sub_RShift`
- Killed since: `all 26` by `tests/unit/test_execution_graph_dispatch.py::test_the_frontier_shard_mapping_is_pinned_for_fixed_run_ids`, `tests/unit/test_execution_graph_dispatch.py::test_other_power_of_two_shard_counts_use_their_own_leading_bits`
- Verdict: KILLED
## S-72 · OPEN · run_may_terminate mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:769
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-73 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:786
- Mutants: 2 surviving at this location
- Mutation: `ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-74 · OPEN · ClaimView mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:797
- Mutants: 1 surviving at this location
- Mutation: `ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-75 · OPEN · _claimable_run mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:821
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-76 · OPEN · _claimable_run mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:822
- Mutants: 2 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-77 · OPEN · decide_claim mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:830
- Mutants: 1 surviving at this location
- Mutation: `ReplaceAndWithOr`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-78 · OPEN · decide_claim mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:832
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-79 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:868
- Mutants: 3 surviving at this location
- Mutation: `RemoveDecorator, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-80 · OPEN · PromoterView mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:879
- Mutants: 1 surviving at this location
- Mutation: `ReplaceFalseWithTrue`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-81 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:905
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_IsNot_Is`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-82 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:906
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_IsNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-83 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:907
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_IsNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-84 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:908
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_IsNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-85 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:909
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-86 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:911
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceAndWithOr`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-87 · OPEN · decide_promotion mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:913
- Mutants: 10 surviving at this location
- Mutation: `AddNot, ReplaceAndWithOr, ReplaceComparisonOperator_IsNot_Is, ReplaceComparisonOperator_Lt_Eq, ReplaceComparisonOperator_Lt_Gt, ReplaceComparisonOperator_Lt_GtE, ReplaceComparisonOperator_Lt_Is, ReplaceComparisonOperator_Lt_IsNot, ReplaceComparisonOperator_Lt_LtE, ReplaceComparisonOperator_Lt_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-88 · OPEN · table_writer_acceptance mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:940
- Mutants: 1 surviving at this location
- Mutation: `ReplaceBinaryOperator_Mul_Div`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-89 · OPEN · table_writer_acceptance mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:943
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-90 · OPEN · table_writer_acceptance mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:944
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-91 · OPEN · table_writer_acceptance mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:947
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-92 · OPEN · table_writer_acceptance mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:948
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-93 · OPEN · table_writer_acceptance mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:951
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-94 · OPEN · legacy_finalize_needs_mode_check mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:958
- Mutants: 7 surviving at this location
- Mutation: `ReplaceComparisonOperator_Is_Eq, ReplaceComparisonOperator_Is_Gt, ReplaceComparisonOperator_Is_GtE, ReplaceComparisonOperator_Is_IsNot, ReplaceComparisonOperator_Is_Lt, ReplaceComparisonOperator_Is_LtE, ReplaceComparisonOperator_Is_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-95 · OPEN · _canonical mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:975
- Mutants: 2 surviving at this location
- Mutation: `ReplaceFalseWithTrue, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-96 · OPEN · bind_graph_envelope mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:995
- Mutants: 3 surviving at this location
- Mutation: `ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-97 · OPEN · provider_accounted_bytes mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1011
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-98 · OPEN · <module> mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1018
- Mutants: 3 surviving at this location
- Mutation: `RemoveDecorator, ReplaceTrueWithFalse, ReplaceTrueWithFalse`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-99 · OPEN · split_roster mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1039
- Mutants: 1 surviving at this location
- Mutation: `ZeroIterationForLoop`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-100 · OPEN · split_roster mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1043
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_LtE_Eq, ReplaceComparisonOperator_LtE_Gt, ReplaceComparisonOperator_LtE_GtE, ReplaceComparisonOperator_LtE_Is, ReplaceComparisonOperator_LtE_IsNot, ReplaceComparisonOperator_LtE_Lt, ReplaceComparisonOperator_LtE_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-101 · OPEN · split_roster mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1045
- Mutants: 1 surviving at this location
- Mutation: `ReplaceContinueWithBreak`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-102 · OPEN · split_roster mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1046
- Mutants: 2 surviving at this location
- Mutation: `AddNot, ReplaceUnaryOperator_Delete_Not`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-103 · OPEN · split_roster mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1055
- Mutants: 8 surviving at this location
- Mutation: `AddNot, ReplaceComparisonOperator_Gt_Eq, ReplaceComparisonOperator_Gt_GtE, ReplaceComparisonOperator_Gt_Is, ReplaceComparisonOperator_Gt_IsNot, ReplaceComparisonOperator_Gt_Lt, ReplaceComparisonOperator_Gt_LtE, ReplaceComparisonOperator_Gt_NotEq`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
## S-104 · OPEN · split_roster mutation survivor
- Track: mechanical
- Location: src/domain/operations/execution_graph_dispatch.py:1058
- Mutants: 1 surviving at this location
- Mutation: `AddNot`
- Survives because: the review records the test outcome only; behavior and equivalence triage are pending.
- Verdict: OPEN
