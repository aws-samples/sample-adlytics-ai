# Mutation review — src/domain/presentation/campaign_portfolio.py

- Commit: 7969ef4 (branch `feat/insight-data-ux-refresh`), reviewed lines: every line changed since `237df7c`
- Date: 2026-10-06
- Tool: cosmic-ray 8.7.0, `cr-filter-git` with `branch = "237df7c"`
- Command: `cosmic-ray exec` in a scratch worktree of the commit, where the test command was `python -m pytest tests/unit/test_campaign_portfolio.py tests/unit/test_contribution_scope.py tests/unit/test_deep_insight_inputs.py tests/unit/test_campaign_preset_source.py tests/unit/test_retained_campaign_artifact_materializer.py -q -x`
- Mutants on touched lines: 158. First run: 33 survived. Second run: 128 killed, 30 survived; 2 of those (`== 'asc'` to `is 'asc'`) are killed since by a test proven against the hand-applied mutant. 28 remain, all equivalent.

## Killed by tests added in this review (`tests/unit/test_contribution_scope.py`)

| Location | Mutation | Killed by |
|---|---|---|
| `campaign_portfolio.py:759` | `pagination is None` negated | `test_a_portfolio_without_pagination_reads_its_own_population` (an empty outcome, the only one without pagination) |
| `campaign_portfolio.py:789` | `or` to `and` | `test_a_portfolio_without_totals_has_no_spend_split` |
| `campaign_portfolio.py:753–754` | direction `==` to `is` | `test_sort_words_compare_the_direction_by_value` (a parsed direction is an equal, non-identical string) |

## Equivalent survivors

| Location | Mutants | Mutation class | Why equivalent | Verdict |
|---|---:|---|---|---|
| `campaign_portfolio.py:179` | 2 | `is not RATIO` to `!=`, `<` | Every other `UnitKind` value sorts before `"ratio"`. | EQUIVALENT |
| `campaign_portfolio.py:539` | 2 | `Decimal(0)` to `±1` in the undefined-ratio sort key | One constant for every undefined row; ties fall to the sort key. | EQUIVALENT |
| `campaign_portfolio.py:541`, `:753`, `:754` | 3 | direction `==` to `>=`/`<=` | Directions are exactly `"asc"` and `"desc"`. | EQUIVALENT |
| `campaign_portfolio.py:752`, `:787` | 4 | enum `is` to `==`, `>=`, `<=` | Singletons; `SPEND` is the first total of the fixed roster, so the order forms find the same item. | EQUIVALENT |
| `campaign_portfolio.py:772` | 2 | `count == population` to `>=`, `is` | N never exceeds M; equal counts are at most 50 (cached ints). | EQUIVALENT |
| `campaign_portfolio.py:775` | 2 | `start = 0` to `±1` when pagination is absent | Pagination is absent only with no rows, which takes the "all N" branch. | EQUIVALENT |
| `campaign_portfolio.py:777` | 2 | `start + 1` to `start | 1`, `start ^ 1` | `start` is a multiple of the fixed segment size 50, so its low bit is 0. | EQUIVALENT |
| `campaign_portfolio.py:784` | 11 | `|` in the return annotation | Postponed annotations. | EQUIVALENT |
