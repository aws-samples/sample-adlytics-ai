# Mutation review — src/domain/presentation/metric_focus.py

- Commit: 7969ef4 (branch `feat/insight-data-ux-refresh`), reviewed lines: every line changed since `237df7c`
- Date: 2026-10-06
- Tool: cosmic-ray 8.7.0, `cr-filter-git` with `branch = "237df7c"`
- Command: `cosmic-ray exec` in a scratch worktree of the commit, where the test command was `python -m pytest tests/unit/test_metric_focus.py tests/unit/test_focus_share_rule.py tests/unit/test_deep_insight_inputs.py tests/unit/test_insight_finding_units.py tests/unit/test_returns_context.py tests/unit/test_contribution_scope.py tests/unit/test_campaign_insight_predicates.py -q -x`
- Mutants on touched lines: 589. First run (before this review's tests): 274 survived, including the share rule itself (`outside_net = total_change + scope_net` survived because no case had same-sign nets with an opposing outside move). Second run: 351 killed, 238 survived; 1 of those is killed since (`comparison != -1`, proven against the hand-applied mutant). 237 remain, all equivalent.

## Killed by tests added in this review (`tests/unit/test_focus_share_rule.py`)

| Location | Mutation | Killed by |
|---|---|---|
| `metric_focus.py:442–445` | scope-net and outside-net sign tests, `outside_net` arithmetic | `test_the_share_rule_holds_at_its_sign_and_size_edges` (opposing outside on same-sign nets, a falling scope beside a rising total, outside moves of ±0.50) |
| `metric_focus.py:458`, `:460`, `:467`, `:493` | share and against-basis sign tests, `>` to `>=` at the basis | same test (negative bases, fractional drivers, a basis of exactly ±1, a driver equal to its basis) |
| `metric_focus.py:534` | rank key `-abs` to `not abs` | `test_drivers_rank_by_size_of_change_not_by_name` |
| `metric_focus.py:571` | `count - len(ranked)` to `%`, `^` | same test (9 candidates, 4 listed: 5, not 1 or 13) |
| `metric_focus.py:471` | product noun `is` to `>` | `test_product_summary_lines_use_the_product_noun` |
| `metric_focus.py:248` | bool `!=` to `<`, `>` in the scope rule | `test_the_scope_rule_refuses_each_half_of_a_mismatched_declaration` |
| `metric_focus.py:217`, `:221`, `:252`, `:260` | default `False` to `True`; `frozen=True` to `False` | `test_driver_records_are_immutable_and_default_to_no_chip` |
| `metric_focus.py:746–747` | `or` to `and`, grain `is not` to `<`, `>` | `test_all_product_drivers_exist_only_for_served_product_candidates` |

## Equivalent survivors

| Location | Mutants | Mutation class | Why equivalent | Verdict |
|---|---:|---|---|---|
| `metric_focus.py:440, 450, 483, 484, 518, 627, 707, 738, 873, 884, 888, 983, 992` | 198 | `|` in annotations | Postponed annotations. | EQUIVALENT |
| `metric_focus.py:217`, `:218` | 2 | `kw_only=True` to `False` | Only widens the call signature; both fields keep defaults. | EQUIVALENT |
| `metric_focus.py:221`, `:252`, `:260` | 3 | `slots=True` to `False` | Frozen still refuses assignment. | EQUIVALENT |
| `metric_focus.py:248`, `:445`, `:458`, `:467` | 5 | bool `!=`/`==` to `is not`/`is` | Bool singletons. | EQUIVALENT |
| `metric_focus.py:445`, `:458`, `:467` | 6 | `> 0` to `>= 0` | The operand is already known non-zero at that point (`scope_net == 0` returns, `outside_net == 0` short-circuits, `change != 0` and `comparison != 0` guard). | EQUIVALENT |
| `metric_focus.py:471, 477, 500, 553, 583, 713, 715, 746` | 14 | enum `is`/`is not` to `==`, `!=`, `>=`, `<=` | Singletons; the order forms select the same member of these closed enums among the grains each function is called with (campaign and product only for `_noun` and `_scope_label`). | EQUIVALENT |
| `metric_focus.py:502`, `:561`, `:562` | 6 | `==` to `>=`/`<=`, `is` | N never exceeds M, the listed count never exceeds N, and both are at most 50 when equal (cached ints). | EQUIVALENT |
| `metric_focus.py:519` | 1 | keyword-only marker `*` mutated | Not an expression. | EQUIVALENT |
| `metric_focus.py:555` | 1 | `candidates[0]` to `candidates[-1]` | Every candidate of one focus shares its metric's units. | EQUIVALENT |
| `metric_focus.py:1245` | 1 | `if candidates` negated | Retail rows all carry the metric, so the population equals the candidate count; with no candidates the drivers are absent. | EQUIVALENT |
