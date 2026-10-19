# Mutation review — src/domain/presentation/insights.py

- Commit: 7969ef4 (branch `feat/insight-data-ux-refresh`), reviewed lines: every line changed since `237df7c`
- Date: 2026-10-06
- Tool: cosmic-ray 8.7.0, `cr-filter-git` with `branch = "237df7c"`, so only mutants on touched lines run
- Command: `cosmic-ray exec` in a scratch worktree of the commit, where the test command was `python -m pytest tests/unit/test_insight_domain_edges.py tests/unit/test_campaign_insights.py tests/unit/test_insight_materiality_cases.py tests/unit/test_insight_next_steps.py tests/unit/test_insight_request_identity.py tests/unit/test_insight_cache.py tests/unit/test_insight_finding_units.py tests/unit/test_campaign_insight_predicates.py tests/unit/test_insight_quiet_reasons.py tests/unit/test_focus_share_rule.py tests/unit/test_deep_insight_inputs.py tests/unit/test_insight_assembly.py tests/unit/test_insight_bedrock.py tests/unit/test_insight_continuation_codec.py tests/unit/test_retail_health.py -q -x`
- Mutants on touched lines: 437. First run (without `test_insight_domain_edges.py`): 85 survived. Second run: 377 killed, 61 survived; 3 of those are killed since by tests proven against the hand-applied mutant. 58 remain, all equivalent.

## Killed by tests added in this review

| Location | Mutation | Why it survived | Killed by |
|---|---|---|---|
| `insights.py:333`, `:1234` | `ErrorCode("INSIGHT", 13/15)` renumbered | No test pinned the telemetry number | `test_the_new_insight_error_codes_keep_their_numbers` |
| `insights.py:338` | threshold `value <= 0` to `== 0` | No negative threshold was refused | `test_a_threshold_must_be_positive_and_a_near_zero_bound_may_be_zero` |
| `insights.py:392` | near-zero bound `< 0` to `<= 0` | A zero bound was never declared | same test |
| `insights.py:363, 377, 396, 1049, 1064, 1756` | `frozen=True` to `False` | No immutability assertion | `test_materiality_and_next_step_records_are_immutable` |
| `insights.py:1013` | duplicate next-step kinds `!=` to `>` | No duplicate-kind contract was refused | `test_a_contract_refuses_a_repeated_next_step_kind` |
| `insights.py:1045`, `:1059` | `or` to `and` in target and id checks | Blank target or blank id never tried | `test_next_step_targets_and_ids_are_structurally_required` |
| `insights.py:1092` | id `==` to `>=`, then `is` | An id sorting before every option, and a parsed (non-identical) id, never tried | `test_an_id_that_sorts_before_every_option_is_still_refused`, `test_a_parsed_step_id_resolves_by_value` |
| `insights.py:1116` | `or` to `and` in stored-step field types | Only an unknown kind was tried | `test_a_stored_step_with_a_non_text_field_is_malformed` |
| `insights.py:1743`, `:1744` | rate prior `* 100` to `* 99/101`; `<=` to `<` | No prior exactly at or just above the bound | `test_a_rate_prior_at_its_near_zero_bound_is_suppressed_and_just_above_it_is_not` |
| `insights.py:1779` | `>=` to `>` in the near-zero quiet reason | No percentage exactly at its threshold | same test |
| `insights.py:1919` | `ensure_ascii` and `sort_keys` flipped | The canonical form was never pinned | `test_the_canonical_input_form_is_sorted_compact_and_unescaped` |
| `insights.py:1928` | digest length `==` to `>=` | Only a 63-character digest was tried | `test_an_input_digest_is_exactly_64_characters` |
| `insights.py:2060` | duplicate option ids `!=` to `>` | Never tried | `test_a_generation_refuses_repeated_next_step_ids` |
| `insights.py:2064` | deep-tier options `is not` to `<` | Options on a deep request never tried | `test_only_the_card_tier_offers_next_steps` |
| `insights.py:2066` | digest check widened to the deep tier; `!=` to `<` | No deep request with enriched input, no digest greater than the real one | `test_a_deep_generation_keeps_the_card_digest_beside_its_enriched_input`, `test_a_card_generation_refuses_any_digest_but_its_own` |

## Equivalent survivors

`from __future__ import annotations` keeps annotations unevaluated, enum members are singletons, and the closed enums' string order is fixed, so these mutants cannot change behaviour.

| Location | Mutants | Mutation class | Why equivalent | Verdict |
|---|---:|---|---|---|
| `insights.py:363, 377, 396, 1049, 1064, 1756` | 6 | `slots=True` to `False` | Only allows new attributes; frozen still refuses assignment. | EQUIVALENT |
| `insights.py:416` | 1 | keyword-only marker `*` mutated | Not an expression; the signature is unchanged at runtime. | EQUIVALENT |
| `insights.py:444` | 2 | `is UNIT_PRICE` to `==`, `>=` | `"unit_price"` is the greatest `MetricScale` value. | EQUIVALENT |
| `insights.py:1013`, `:2060` | 4 | length `!=` to `<`, `is not` | A set is never longer than its source; small ints are cached. | EQUIVALENT |
| `insights.py:1041`, `:1770` | 22 | `|` in return or parameter annotations | Postponed annotations. | EQUIVALENT |
| `insights.py:1226`, `:1729`, `:1742`, `:1812`, `:1975`, `:2066` | 8 | enum `is` to `==`, `>=`, `<=` | Singletons; the order-based forms select the same member in these closed enums. | EQUIVALENT |
| `insights.py:1430` | 1 | `|` to `^` on two disjoint frozensets | Union and symmetric difference agree on disjoint sets. | EQUIVALENT |
| `insights.py:1651`, `:1691`, `:1764` | 4 | bool `==`/`!=` to `is`/`is not` | `True` and `False` are singletons. | EQUIVALENT |
| `insights.py:1738` | 2 | `is not PERCENTAGE_CHANGE` to `!=`, `<` | Only percentage findings carry a prior scale and magnitude in every builder; the other kinds return at the `prior_magnitude is None` clause. | EQUIVALENT |
| `insights.py:1833` | 1 | `== 1` to `<= 1` | A counted name occurs at least once. | EQUIVALENT |
| `insights.py:1877–1885` | 5 | `@property` removed on the `InsightInputSource` protocol | A protocol is structural and checked statically only. | EQUIVALENT |
| `insights.py:2064` | 2 | `is not CARD` to `!=`, `>` | Two tiers; both forms select the deep tier. | EQUIVALENT |
