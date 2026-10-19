# Mutation review — src/domain/conversation/semantic_verification.py

- Commit: 1a297469f148c26670ef90b29ca4c100e04dc69e
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/conversation/semantic_verification.py TESTS=tests/unit/test_semantic_verification.py`
- Mutants: 171 generated, 122 killed, 49 survived
- Survivors: 49 open, 0 killed since, 0 equivalent

Scope caveat: owner test file only; consumer suites (test_agentcore_harness.py,
test_managed_conversation_runtime.py, test_presentation_finalizer.py) were excluded
and exercise this module's verify/materialize path on every completion. Line-level
locations are unavailable because `make mutate` deletes the run database before the
next run; only this operator/occurrence inventory was retained. Rerun with a kept
database before strengthening tests. Entries group one cause across locations.

## S-01 · OPEN · violation-matrix comparisons under-pinned per operator
- Track: mechanical
- Location: verify_presentation_plan / materialize_verified_plan (lines pending rerun)
- Mutants: comparison-operator rows in the inventory
- Mutation: `==` and `!=` on IDs and digests become ordering/identity variants
- Survives because: accept/reject outcomes are pinned for canonical paths, but
  duplicate-ID, digest-mismatch, and materialize-guard branches lack a per-operator
  counterexample; note `Eq -> Is` on identical short strings may be CPython-interning
  equivalent and needs per-mutant confirmation before any EQUIVALENT verdict
- Verdict: OPEN

## S-02 · OPEN · evidence loops emptied without objection
- Track: mechanical
- Location: obligations/collection folds (ZeroIterationForLoop rows)
- Mutants: 2 surviving
- Mutation: a `for` over grounding evidence iterates zero times
- Survives because: some obligation folds contribute nothing to the asserted outcomes
  in the owner tests
- Verdict: OPEN

## Untriaged survivor inventory (operator/occurrence; lines pending rerun)

49 surviving mutants:

| # | mutant |
|---|---|
| 1 | `ReplaceBinaryOperator_Sub_BitXor 1` |
| 2 | `ReplaceComparisonOperator_Eq_LtE 1` |
| 3 | `ReplaceComparisonOperator_Eq_GtE 1` |
| 4 | `ReplaceComparisonOperator_Eq_Is 0` |
| 5 | `ReplaceComparisonOperator_Eq_Is 1` |
| 6 | `ReplaceComparisonOperator_NotEq_Lt 0` |
| 7 | `ReplaceComparisonOperator_NotEq_Lt 1` |
| 8 | `ReplaceComparisonOperator_NotEq_Lt 2` |
| 9 | `ReplaceComparisonOperator_NotEq_Lt 3` |
| 10 | `ReplaceComparisonOperator_NotEq_Gt 0` |
| 11 | `ReplaceComparisonOperator_NotEq_Gt 1` |
| 12 | `ReplaceComparisonOperator_NotEq_Gt 2` |
| 13 | `ReplaceComparisonOperator_NotEq_Gt 3` |
| 14 | `ReplaceComparisonOperator_NotEq_Is 4` |
| 15 | `ReplaceComparisonOperator_NotEq_IsNot 0` |
| 16 | `ReplaceComparisonOperator_NotEq_IsNot 1` |
| 17 | `ReplaceComparisonOperator_NotEq_IsNot 2` |
| 18 | `ReplaceComparisonOperator_NotEq_IsNot 3` |
| 19 | `ReplaceComparisonOperator_Is_Eq 0` |
| 20 | `ReplaceComparisonOperator_Is_Eq 1` |
| 21 | `ReplaceComparisonOperator_Is_LtE 1` |
| 22 | `ReplaceComparisonOperator_Is_Gt 0` |
| 23 | `ReplaceComparisonOperator_Is_GtE 0` |
| 24 | `ReplaceComparisonOperator_Is_GtE 1` |
| 25 | `ReplaceComparisonOperator_Is_IsNot 5` |
| 26 | `ReplaceComparisonOperator_IsNot_NotEq 0` |
| 27 | `ReplaceComparisonOperator_IsNot_Lt 0` |
| 28 | `ReplaceComparisonOperator_IsNot_Gt 0` |
| 29 | `ReplaceUnaryOperator_Delete_Not 5` |
| 30 | `AddNot 10` |
| 31 | `AddNot 21` |
| 32 | `AddNot 22` |
| 33 | `ReplaceTrueWithFalse 0` |
| 34 | `ReplaceTrueWithFalse 1` |
| 35 | `ReplaceTrueWithFalse 3` |
| 36 | `ReplaceTrueWithFalse 4` |
| 37 | `ReplaceTrueWithFalse 5` |
| 38 | `ReplaceTrueWithFalse 6` |
| 39 | `ReplaceTrueWithFalse 7` |
| 40 | `ReplaceTrueWithFalse 9` |
| 41 | `ReplaceTrueWithFalse 10` |
| 42 | `ReplaceFalseWithTrue 0` |
| 43 | `ReplaceFalseWithTrue 1` |
| 44 | `ReplaceAndWithOr 1` |
| 45 | `ReplaceAndWithOr 3` |
| 46 | `ReplaceOrWithAnd 0` |
| 47 | `ReplaceOrWithAnd 1` |
| 48 | `ZeroIterationForLoop 0` |
| 49 | `ZeroIterationForLoop 1` |
