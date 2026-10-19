# Mutation review — src/domain/conversation/tool_custody.py

- Commit: 1a297469f148c26670ef90b29ca4c100e04dc69e
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/conversation/tool_custody.py TESTS=tests/unit/test_tool_custody.py`
- Mutants: 100 generated, 22 killed, 78 survived
- Survivors: 78 open, 0 killed since, 0 equivalent

Scope caveat — this number is dominated by a scoping artifact, not a blind test
suite: the owner test file holds only the four disposition-construction regressions,
while the digest projection and custody assembly are exercised by the excluded
consumer suites (test_agentcore_harness.py, test_managed_conversation_runtime.py,
conversation_publication_fixture.py). Line-level locations are unavailable because
the run database was deleted by the next `make mutate` run. Rerun with the consumer
suites and a kept database before treating individual rows as findings.

## S-01 · OPEN · digest projection unexercised under owner-scoped command
- Track: mechanical
- Location: `_digest` and `AgentToolCustodyV1` (lines pending rerun)
- Mutants: majority of the 78 (loop, boolean, and operator rows in the inventory)
- Mutation: projection loops emptied, serializer flags flipped, concatenation altered
- Survives because: the owner tests never assert digest bytes or custody assembly;
  consumers do, but were out of scope for this run
- Verdict: OPEN

## S-02 · OPEN · construction validations partially pinned
- Track: mechanical
- Location: `ValidatedToolResultV1.__post_init__` (lines pending rerun)
- Mutants: boolean/comparison rows in the inventory
- Mutation: validation conditions weakened
- Survives because: the four regressions pin the disposition/grounding matrix but not
  ordinal, tool-name, and contract-version validations
- Verdict: OPEN

## Untriaged survivor inventory (operator/occurrence; lines pending rerun)

78 surviving mutants:

| # | mutant |
|---|---|
| 1 | `ReplaceBinaryOperator_Add_Sub 0` |
| 2 | `ReplaceBinaryOperator_Add_Sub 1` |
| 3 | `ReplaceBinaryOperator_Add_Mul 0` |
| 4 | `ReplaceBinaryOperator_Add_Mul 1` |
| 5 | `ReplaceBinaryOperator_Add_Div 0` |
| 6 | `ReplaceBinaryOperator_Add_Div 1` |
| 7 | `ReplaceBinaryOperator_Add_FloorDiv 0` |
| 8 | `ReplaceBinaryOperator_Add_FloorDiv 1` |
| 9 | `ReplaceBinaryOperator_Add_Mod 0` |
| 10 | `ReplaceBinaryOperator_Add_Mod 1` |
| 11 | `ReplaceBinaryOperator_Add_Pow 0` |
| 12 | `ReplaceBinaryOperator_Add_Pow 1` |
| 13 | `ReplaceBinaryOperator_Add_RShift 0` |
| 14 | `ReplaceBinaryOperator_Add_RShift 1` |
| 15 | `ReplaceBinaryOperator_Add_LShift 0` |
| 16 | `ReplaceBinaryOperator_Add_LShift 1` |
| 17 | `ReplaceBinaryOperator_Add_BitOr 0` |
| 18 | `ReplaceBinaryOperator_Add_BitOr 1` |
| 19 | `ReplaceBinaryOperator_Add_BitAnd 0` |
| 20 | `ReplaceBinaryOperator_Add_BitAnd 1` |
| 21 | `ReplaceBinaryOperator_Add_BitXor 0` |
| 22 | `ReplaceBinaryOperator_Add_BitXor 1` |
| 23 | `ReplaceComparisonOperator_NotEq_Eq 0` |
| 24 | `ReplaceComparisonOperator_NotEq_Eq 1` |
| 25 | `ReplaceComparisonOperator_NotEq_Eq 2` |
| 26 | `ReplaceComparisonOperator_NotEq_Lt 0` |
| 27 | `ReplaceComparisonOperator_NotEq_Lt 1` |
| 28 | `ReplaceComparisonOperator_NotEq_Lt 2` |
| 29 | `ReplaceComparisonOperator_NotEq_LtE 0` |
| 30 | `ReplaceComparisonOperator_NotEq_LtE 1` |
| 31 | `ReplaceComparisonOperator_NotEq_LtE 2` |
| 32 | `ReplaceComparisonOperator_NotEq_Gt 0` |
| 33 | `ReplaceComparisonOperator_NotEq_Gt 1` |
| 34 | `ReplaceComparisonOperator_NotEq_Gt 2` |
| 35 | `ReplaceComparisonOperator_NotEq_GtE 0` |
| 36 | `ReplaceComparisonOperator_NotEq_GtE 1` |
| 37 | `ReplaceComparisonOperator_NotEq_GtE 2` |
| 38 | `ReplaceComparisonOperator_NotEq_Is 0` |
| 39 | `ReplaceComparisonOperator_NotEq_Is 1` |
| 40 | `ReplaceComparisonOperator_NotEq_Is 2` |
| 41 | `ReplaceComparisonOperator_NotEq_IsNot 0` |
| 42 | `ReplaceComparisonOperator_NotEq_IsNot 1` |
| 43 | `ReplaceComparisonOperator_NotEq_IsNot 2` |
| 44 | `ReplaceComparisonOperator_Lt_NotEq 0` |
| 45 | `ReplaceComparisonOperator_Lt_Gt 0` |
| 46 | `ReplaceComparisonOperator_Is_Eq 0` |
| 47 | `ReplaceComparisonOperator_Is_GtE 0` |
| 48 | `ReplaceComparisonOperator_Is_IsNot 1` |
| 49 | `ReplaceComparisonOperator_Is_IsNot 4` |
| 50 | `ReplaceComparisonOperator_IsNot_Is 1` |
| 51 | `AddNot 5` |
| 52 | `AddNot 6` |
| 53 | `AddNot 7` |
| 54 | `AddNot 8` |
| 55 | `AddNot 9` |
| 56 | `AddNot 10` |
| 57 | `ReplaceTrueWithFalse 0` |
| 58 | `ReplaceTrueWithFalse 1` |
| 59 | `ReplaceTrueWithFalse 2` |
| 60 | `ReplaceTrueWithFalse 3` |
| 61 | `ReplaceTrueWithFalse 4` |
| 62 | `ReplaceTrueWithFalse 5` |
| 63 | `ReplaceTrueWithFalse 6` |
| 64 | `ReplaceFalseWithTrue 0` |
| 65 | `ReplaceFalseWithTrue 1` |
| 66 | `ReplaceFalseWithTrue 2` |
| 67 | `ReplaceFalseWithTrue 3` |
| 68 | `ReplaceFalseWithTrue 4` |
| 69 | `NumberReplacer 1` |
| 70 | `NumberReplacer 2` |
| 71 | `NumberReplacer 3` |
| 72 | `NumberReplacer 4` |
| 73 | `NumberReplacer 5` |
| 74 | `NumberReplacer 6` |
| 75 | `NumberReplacer 7` |
| 76 | `RemoveDecorator 1` |
| 77 | `RemoveDecorator 2` |
| 78 | `ZeroIterationForLoop 0` |
