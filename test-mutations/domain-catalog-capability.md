# Mutation review — src/domain/catalog/capability.py

- Commit: 6aa89dc5ee5f8c46a7f17e50d7e92a1732ed625f
- Date: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Command: `uv run cosmic-ray exec .mutation-capability-retained.toml /tmp/capability-retained.sqlite`, where the test command was `python -m pytest tests/unit/test_catalog_tool.py tests/unit/test_planning.py tests/unit/test_resolved_analytical_request.py tests/unit/test_page_schemas.py -q -x`
- Mutants: 455 generated, 301 killed, 154 survived
- Survivors: 85 open, 8 killed since, 69 equivalent

This is the retained-database rerun requested after the initial owner-only review. It includes the catalog owner suite and the direct planning, semantic-resolution, and page-schema consumers. The SQLite database was retained only in `/tmp` for this review and is not a repository artifact.

Notable negative result: `metric_candidates` and `deferred_candidates` — the resolution surfaces added for ADR-062 — produced no surviving behavioral mutation. The error-code mutations recorded as S-01 in the prior review are now killed by the literal contract tests added in commit `6aa89dc`.

## S-01 · KILLED · CATALOG wire error-code numbers are pinned
- Track: mechanical
- Location: src/domain/catalog/capability.py:125-128
- Mutants: 8 formerly surviving at these four constants
- Mutation: `ErrorCode(area="CATALOG", number=1)` becomes another number
- Survived because: the original tests asserted refusal reason and error class but never the numeric telemetry dimension
- Killed by: `tests/unit/test_catalog_tool.py::TestRefusals::test_each_catalog_refusal_reason_has_its_stable_public_error_code[case0]`, `[case1]`, `[case2]`, and `[case3]`
- Verdict: KILLED

## Equivalent survivors

Each row is a separate location; no entry groups locations. `from __future__ import annotations` means union annotations are stored unevaluated, so changing the annotation operator cannot alter runtime behavior. Enum members are singletons, so `is` to `==` is equivalent for the closed values in these comparisons. The `TYPE_CHECKING` and `RefusalReason >= UNKNOWN_METRIC` cases are also equivalent under this module's current closed import and enum contracts.

| Location | Mutants | Mutation class | Why equivalent | Verdict |
|---|---:|---|---|---|
| `capability.py:39` | 1 | `TYPE_CHECKING` guard negated | The runtime-only import of `Mapping` changes no exported or executed catalog behavior under postponed annotations. | EQUIVALENT |
| `capability.py:183` | 2 | `is` to `==`; `is` to `>=` | `UNKNOWN_METRIC` is the lexicographically greatest current closed refusal value; both predicates select only it. | EQUIVALENT |
| `capability.py:318` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:320` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:322` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:324` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:327` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:372` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:561` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:607` | 11 | return `Metric | None` annotation operators | Postponed annotation only. | EQUIVALENT |
| `capability.py:649` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:679` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:710` | 11 | `Mapping | None` parameter annotation operators | Postponed annotation only. | EQUIVALENT |
| `capability.py:746` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |
| `capability.py:771` | 11 | `str | None` parameter annotation operators | Postponed annotation only. | EQUIVALENT |
| `capability.py:772` | 11 | `Mapping | None` parameter annotation operators | Postponed annotation only. | EQUIVALENT |
| `capability.py:773` | 11 | return `Refusal | None` annotation operators | Postponed annotation only. | EQUIVALENT |
| `capability.py:844` | 1 | `MetricKind is` to `==` | Enum singleton equality is identical. | EQUIVALENT |

## Open survivors

Every row is one exact source location. These are behavior-changing candidates, not a score-improvement backlog. The next test change should be selected by business impact, beginning with dictionary acceptance and evidence/qualification semantics.

| Location | Mutants | Mutation class | Survives because | Business risk | Verdict |
|---|---:|---|---|---|---|
| `capability.py:138` | 2 | `Refusal` dataclass flags | No test attempts to mutate a constructed refusal. | A supposedly immutable refusal could be rewritten after construction. | OPEN |
| `capability.py:188` | 2 | `Metric` dataclass flags | No test attempts to mutate a constructed metric. | A metric declaration could change after validation. | OPEN |
| `capability.py:206` | 2 | `profile_version` default | No test uses the default and asserts its version semantics. | Declaration profile provenance can silently change. | OPEN |
| `capability.py:244` | 1 | `non_additive` default | No test constructs a defaulted metric and asserts additivity. | A distinct/non-additive metric could be treated as summable. | OPEN |
| `capability.py:269` | 1 | `declared_mixes_basis` default | No test pins the default false declaration. | A measurement-basis caveat could be emitted or omitted incorrectly. | OPEN |
| `capability.py:299` | 3 | required-selector-key uniqueness | The duplicate test does not distinguish all weakened comparisons. | Invalid selector contracts may load and later resolve ambiguously. | OPEN |
| `capability.py:304` | 4 | required-selector-values validation | Only one malformed selector shape is covered. | Empty or duplicate selector alternatives can enter the catalog. | OPEN |
| `capability.py:318` | 2 | ratio operand validation | Existing ratio fixture does not probe every malformed operand combination. | A ratio can load without a complete governed expression. | OPEN |
| `capability.py:320` | 2 | simple definition validation | Existing fixture does not probe every falsy/invalid definition path. | A simple metric can load without a computable definition. | OPEN |
| `capability.py:322` | 2 | simple aggregation-policy validation | Existing fixture does not probe every absent-policy path. | A simple metric can load without its aggregation semantics. | OPEN |
| `capability.py:324` | 2 | ratio aggregation-policy rejection | Existing fixture does not distinguish every non-null ratio policy. | A ratio can carry duplicate aggregation authority. | OPEN |
| `capability.py:327` | 2 | simple-metric compatibility constraint | No non-strict simple-metric counterexample. | A leaf metric can opt into invalid compatibility behavior. | OPEN |
| `capability.py:328` | 3 | simple compatibility enum validation | One branch is covered, not all enum substitutions. | The same leaf-compatibility invariant can be weakened. | OPEN |
| `capability.py:331` | 4 | attribution-window lower bound | Boundary tests do not cover zero, negative, and altered bound independently. | Invalid attribution windows can load. | OPEN |
| `capability.py:333` | 4 | lookback-month lower bound | Boundary tests do not cover zero, negative, and altered bound independently. | Invalid lookback declarations can load. | OPEN |
| `capability.py:341` | 3 | multiplier/ration-only constraint | Current tests do not separate kind and multiplier mutations. | A simple metric can be silently rescaled. | OPEN |
| `capability.py:353` | 1 | alias-validation loop emptied | The alias suite does not prove every alias is iterated. | Duplicate or blank normalized aliases can enter resolution. | OPEN |
| `capability.py:374` | 2 | simple-source validation | Tests do not distinguish source absence from source-check bypass. | A simple metric can load without traceable source provenance. | OPEN |
| `capability.py:390` | 1 | `mixes_basis` property decorator removed | No test asserts property access rather than callable access. | Public metric API changes shape and callers can misread a method object as truthy. | OPEN |
| `capability.py:393` | 7 | basis-count comparison | Only the multi-basis case is pinned; zero/one/boundary variants are not. | Reconciliation caveats can be emitted or omitted incorrectly. | OPEN |
| `capability.py:418` | 2 | `InventoryCategory` dataclass flags | No test attempts mutation after construction. | Category membership/display metadata can change after validation. | OPEN |
| `capability.py:432` | 1 | display-order lower bound | The exact zero/negative boundary is under-pinned. | Inventory ordering can become invalid. | OPEN |
| `capability.py:438` | 3 | category-membership uniqueness | One duplicate fixture does not distinguish all weakened comparisons. | A category can count one metric twice. | OPEN |
| `capability.py:446` | 2 | `DeferredMetric` dataclass flags | No test attempts mutation after construction. | Deferred roadmap metadata can change after validation. | OPEN |
| `capability.py:467` | 2 | `Catalog` dataclass flags | No test attempts mutation after construction. | The loaded registry can change after invariant checks. | OPEN |
| `capability.py:484` | 1 | computable-name duplicate check | Only one duplicate layout is covered. | Duplicate canonical metrics can be accepted. | OPEN |
| `capability.py:489` | 1 | default-ROAS cardinality | Zero/two-default tests do not kill the mutated comparison. | The system can lose or ambiguously select its governed default ROAS. | OPEN |
| `capability.py:495` | 3 | deferred-name uniqueness | Only one deferred duplicate shape is covered. | Roadmap metrics can resolve nondeterministically. | OPEN |
| `capability.py:530` | 3 | category-id uniqueness | Duplicate category cases do not distinguish weakened checks. | Collections can conflict or overwrite each other. | OPEN |
| `capability.py:533` | 3 | display-order uniqueness | Duplicate-order cases do not distinguish weakened checks. | Canonical inventory order becomes nondeterministic. | OPEN |
| `capability.py:546` | 3 | cross-category metric membership | One repeated-member case does not distinguish weakened checks. | Inventory totals and category ownership can disagree. | OPEN |
| `capability.py:561` | 1 | ratio operand traversal | Ratio branch changes are not fully counterexample-tested. | Unknown operands can reach recursive resolution. | OPEN |
| `capability.py:649` | 1 | simple-vs-ratio fact resolution | Current fixtures cover simple and ratio outcomes, not changed dispatch. | A ratio can report direct facts instead of operand facts. | OPEN |
| `capability.py:679` | 1 | simple-vs-ratio source resolution | Current fixtures cover outputs, not changed dispatch. | A ratio can report incorrect source provenance. | OPEN |
| `capability.py:686` | 1 | raw-operand `continue` becomes `break` | No ratio has a raw operand followed by a declared metric operand. | One raw operand can hide later governed sources. | OPEN |
| `capability.py:746` | 2 | non-additive kind dispatch | Default/ratio inheritance cases do not distinguish ordering substitutions. | Non-additive figures may be summed across an invalid grain. | OPEN |
| `capability.py:770` | 1 | keyword-only boundary `*` becomes `/` | All callers use keywords, so signature widening survives. | Callers can bypass the named-argument contract and swap grain/filter meaning. | OPEN |
| `capability.py:838` | 1 | cycle-completion guard negated | Cycle tests do not expose repeated completed-node traversal. | A valid dependency graph can falsely report a cycle or recurse unnecessarily. | OPEN |
| `capability.py:844` | 2 | ratio-only cycle recursion | No simple/ratio mixed traversal counterexample distinguishes changed comparison. | Cycles can be missed or falsely traversed. | OPEN |
| `capability.py:850` | 1 | cycle-check loop emptied | No test proves every catalog metric enters cycle detection. | A cyclic metric dictionary can load. | OPEN |

## Recommended remediation order

1. **Dictionary integrity:** selector validation, ratio/simple construction, source provenance, category partition, alias iteration, and cycle traversal. These are fail-open catalog-load risks and should receive compact boundary counterexamples.
2. **Measurement truthfulness:** `mixes_basis`, non-additivity, fact/source recursion, and raw-operand continuation. These affect released figures and material qualifiers.
3. **Immutability and API contracts:** dataclass flags, property access, and keyword-only parameters. These are lower-frequency but protect post-validation mutation and call-site meaning.
4. **Version/default metadata:** profile version and defaults after the behavioral invariants are covered.

No source changes were made during this retained review. The report itself is the only repository change pending commit.

## 2026-10-06 — `ratio_scale` (branch `feat/insight-data-ux-refresh`, commit 7969ef4)

The branch added `RatioScale` and the `Metric.ratio_scale` field (13 lines). `cosmic-ray exec` with `cr-filter-git` (`branch = "237df7c"`) and the test command `python -m pytest tests/unit/test_metric_dictionary.py tests/unit/test_catalog_tool.py -q -x` generated no mutant on those lines: they are an enum and a defaulted field declaration. The loader that refuses a missing or misplaced `ratio_scale` lives in `src/adapters/outbound/metric_dictionary.py` and is covered by `tests/unit/test_metric_dictionary.py`. The earlier open rows above are unchanged.
