# Mutation review — src/domain/measurement/figures.py

- Commit: 6710750c131964c7609625d77f6ed299bb0e836f
- Date: 2026-08-22
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/measurement/figures.py TESTS=tests/unit/test_figures.py`
- Mutants: 758 generated, 220 killed, 538 survived
- Survivors: 22 open, 52 killed since, 516 equivalent

First review of this module, and the first outside `domain/identity` and `domain/strategy` — which is what [G-25](../docs/decision-coverage-registry.md#gap-register-g-nn) asks for, because this is where a surviving mutant is a wrong number rather than a wrong redirect.

The initial run left 590 survivors, of which 101 were genuine. 52 are now killed by named tests and the module's tests went from 39 to 53. Of the 49 that remain outside the two original equivalent classes, 27 have been shown equivalent with a stated mechanism and 22 are open.

**The raw survival rate is 72% and it is not the finding.** 484 survivors are `|` in union type annotations and 5 more are `is` versus `==` on enum members. Reporting the score would have buried the 101 that mattered, which is ADR-013's rule about the score doing real work.

**Two of the largest clusters turned out to need no test at all**, and that is the more interesting half of this review. S-01's 22 mutants are unreachable because an invariant one layer up refuses the state they need, and S-09 and S-10 are equivalent by the shape of the values they compare. Writing tests for those would have asserted behaviour the domain forbids. The remaining clusters were a genuine and total absence: `withholding_in` and `Withholding.occurred` had no tests, while the adapter reads `occurred` to decide whether suppression is reported at all.

## S-01 · EQUIVALENT · The multiplier on the single-aggregate path cannot be reached

- Track: mechanical
- Location: src/domain/measurement/figures.py:216
- Mutants: 22 surviving at this location
- Mutation: `return operands[0] * plan.multiplier if plan.multiplier != 1 else operands[0]` — `*` becomes each of `+ - / // % ** << >> & | ^`, `!= 1` becomes `!= 2` / `!= 0` and the comparison flips, and `AddNot` inverts the condition
- Survives because: `Combination.SINGLE` is the simple-metric branch, and `Metric.__post_init__` in `src/domain/catalog/capability.py:274` refuses `multiplier != 1` on any metric that is not a `RATIO` — "only a ratio may carry a multiplier. A simple metric sums a declared column whose contract already applied a scale factor." So no catalog-derived plan can reach this line with a multiplier other than 1, both arms return `operands[0]`, and every mutation of the scaling is unobservable.
- Verdict: EQUIVALENT

Initially recorded OPEN, on the assumption that a test with a scaled simple metric would kill all 22. Checking the dictionary first is what corrected it: `cpm` is the one metric carrying a multiplier and it is a ratio, so it reaches line 308's arithmetic instead. A test here would have had to construct a plan the catalog refuses to produce, which is the same mistake as inventing a semantic mutation that cannot import.

Worth noting rather than acting on: this makes the `* plan.multiplier` arm dead code guarded by an invariant in another module. Removing it would kill 22 mutants, and ADR-013 forbids that — editing the source so a mutation can no longer be applied is not remediation.

## S-02 · KILLED · `Withholding.occurred` had no test, and it gates suppression reporting

- Track: mechanical
- Location: src/domain/measurement/figures.py:292
- Mutants: 6 at this location
- Mutation: `return self.withheld_rows > 0` — `>` becomes `== >= < <=`, and `0` becomes `1` / `-1`
- Survived because: the string `occurred` did not appear in `tests/unit/test_figures.py` at all. The property was never read by a test, so no assertion could observe any of these.
- Verdict: KILLED by `tests/unit/test_figures.py::TestWhatAResultHadWithheld::test_nothing_withheld_is_earned_from_a_count_rather_than_assumed`

This was the most consequential entry despite being smaller than S-01, because the property is live rather than dead: `src/adapters/outbound/athena/query.py:189` reads `if not withholding.occurred:` to decide whether suppression is reported. [ADR-035](../docs/decisions/adr-035-suppression-is-a-quantity-not-a-flag.md) made suppression a measured quantity so that `rows_suppressed=False` is *earned* from a count rather than assumed, and this predicate is where the count becomes that boolean. The new test asserts the earned case — every count zero, `occurred is False` — which is the claim an answer actually makes.

## S-03 · KILLED · Removing `@property` from `occurred` left it always truthy

- Track: mechanical
- Location: src/domain/measurement/figures.py:289
- Mutants: 1 at this location
- Mutation: `@property` is removed
- Survived because: the same absence as S-02. Recorded separately because the failure mode is worse: without the decorator `withholding.occurred` is a bound method, which is truthy, so the adapter's `if not withholding.occurred` is never taken and suppression is silently never reported. Identical in shape to `domain-strategy-objectives.md` S-02, where a truthiness assertion passed on a bound method.
- Verdict: KILLED by `tests/unit/test_figures.py::TestWhatAResultHadWithheld::test_nothing_withheld_is_earned_from_a_count_rather_than_assumed`

Killed by the same test as S-02 and only because it asserts `occurred is False` by identity. `assert not withholding.occurred` would have passed on the bound method and left this alive — the lesson from the objectives review, applied here deliberately rather than rediscovered.

## S-04 · KILLED · The `index == 0` guard that makes the withheld amount single-sided

- Track: mechanical
- Location: src/domain/measurement/figures.py:494
- Mutants: 7 at this location
- Mutation: `if index == 0:` — `==` becomes `!= > >= <`, `0` becomes `1` / `-1`, and `AddNot` inverts it
- Survived because: no test drove `withholding_in` with a two-operand plan whose second operand also carried a withheld amount. With one operand, `index` is only ever 0, so every mutation of this guard selected the same single iteration.
- Verdict: KILLED by `tests/unit/test_figures.py::TestWhatAResultHadWithheld::test_only_the_numerator_amount_is_totalled`

The guard exists because the withheld *amount* is meaningful only for the numerator — withheld spend and withheld revenue are different units, so their sum has none. The new test gives the two operands distinct amounts and asserts only the numerator's is totalled, while the row counts add across both.

## S-05 · KILLED · `withholding_in`'s accumulation was uncovered

- Track: mechanical
- Location: src/domain/measurement/figures.py:483, 484, 485, 486, 487, 488, 490, 491, 492, 496, 498, 499
- Mutants: 18 across these 12 locations
- Mutation: `ZeroIterationForLoop` on both loops (486, 488); the accumulator initialisers `Decimal(0)` / `0` / `0` become 1 and -1 (483, 484, 485); `if not _is_set(count)` inverted (490); `continue` becomes `break` (491); `touched = True` / `False` flipped (492, 487); `if raw is not None` inverted (496); `if touched` inverted (498); `groups += 1` becomes `+= 2` (499)
- Survived because: the function had no tests. Emptying either loop still returned a `Withholding`, and nothing asserted `groups`, `total_groups` or the accumulated `amount`.
- Verdict: KILLED by `tests/unit/test_figures.py::TestWhatAResultHadWithheld::test_only_the_groups_that_lost_something_are_counted`

Recorded as one entry against twelve locations, against the grouping rule's letter, because they shared one cause and one fix. All 18 died to the new test class, so the grouping held; split it per location if any survives a future change.

## S-06 · KILLED · The cross-fact quotient was only ever tested on exact division

- Track: mechanical
- Location: src/domain/measurement/figures.py:308
- Mutants: 4 at this location
- Mutation: `return ((numerator / denominator) * plan.multiplier).quantize(_RATIO_SCALE)` — `*` becomes `/ // **` and `/` becomes `//`
- Survived because: every existing case divided exactly — 400 over 100 — so flooring changed nothing before `quantize`, and no case used a multiplier other than 1, so `* 1`, `/ 1` and `** 1` were indistinguishable.
- Verdict: KILLED by `tests/unit/test_figures.py::TestADrillAcrossRowResolvesOrRefuses::test_a_cross_fact_quotient_keeps_its_fraction` and `tests/unit/test_figures.py::TestADrillAcrossRowResolvesOrRefuses::test_a_multiplier_scales_a_cross_fact_quotient`

`Div_FloorDiv` was the one survivor in this module that would have produced a wrong figure in production rather than an unexercised branch: a floored quotient truncates every non-integral ratio, which is every real ROAS. 400 over 3 is 133.3333 and floors to 133.0000.

Unlike S-01, the multiplier here is reachable: a drill-across metric is a `RATIO`, and a ratio is permitted a multiplier. None declares one today, so the test builds the plan with `replace(..., multiplier=1000)` — pinning the arithmetic before such a metric exists rather than after it reports a figure a thousand times wrong.

## S-07 · EQUIVALENT · Type annotations under postponed evaluation

- Track: mechanical
- Location: 44 locations across the module
- Mutants: 484 surviving across those locations
- Mutation: `ReplaceBinaryOperator_BitOr_*` — the `|` in a union annotation such as `Decimal | None` becomes each of the other eleven binary operators
- Survives because: `from __future__ import annotations` on line 22 makes every annotation a string that is never evaluated at runtime. The first of the two classes ADR-013 names, and 88% of all survivors in this module.
- Verdict: EQUIVALENT

## S-08 · EQUIVALENT · Identity versus equality on an enum member

- Track: mechanical
- Location: src/domain/measurement/figures.py:144, 194, 215, 219, 393
- Mutants: 5 surviving across those locations
- Mutation: `is` becomes `==`, and `is not` becomes `!=`, against `Combination` and `UnitKind` members
- Survives because: enum members are singletons, so identity and equality coincide by construction for every possible operand. No test can distinguish them and none should try.
- Verdict: EQUIVALENT

The *ordering* substitutions at these same lines — `is` becoming `>=` or `<=` — are not in this entry and remain open below. They survive for a different reason: `Combination` is a `StrEnum` whose values order as `difference < ratio < single`, so `>= Combination.SINGLE` happens to coincide with `== Combination.SINGLE` for all three members. That is an accident of the chosen strings rather than a property of enums, and it stops holding the moment a member is added whose value sorts after `single`.

## S-09 · EQUIVALENT · `> 0` and `!= 0` coincide on a sum of row counts

- Track: mechanical
- Location: src/domain/measurement/figures.py:292
- Mutants: 1 surviving at this location
- Mutation: `return self.withheld_rows > 0` becomes `!= 0`
- Survives because: `withheld_rows` accumulates `int(Decimal(str(count)))` over counts the composed statement produces as `SUM(CASE WHEN ... THEN 1 ELSE 0)`, so it cannot be negative. For a non-negative integer `> 0` and `!= 0` select the same values.
- Verdict: EQUIVALENT

The other six mutants at this location are killed under S-02. This one is separated rather than grouped with them because its verdict differs, and a test could only kill it by passing a negative withheld-row count — a value the query layer cannot emit.

## S-10 · EQUIVALENT · `<= 0` selects the same iteration as `== 0`

- Track: mechanical
- Location: src/domain/measurement/figures.py:494
- Mutants: 1 surviving at this location
- Mutation: `if index == 0:` becomes `if index <= 0:`
- Survives because: `index` comes from `range(_operand_count(plan))`, which always starts at 0, so no negative index exists and `<= 0` matches exactly the first iteration. Killing it would need a plan whose operand indices start below zero, which `range` cannot produce.
- Verdict: EQUIVALENT

The other seven mutants at this location are killed under S-04.

## S-11 · KILLED · The error codes were unpinned, and a collision would have been silent

- Track: mechanical
- Location: src/domain/measurement/figures.py:54, 55, 56
- Mutants: 6 across these 3 locations
- Mutation: `NumberReplacer` on `ErrorCode(area="MEASURE", number=1)`, `number=2` and `number=3` — each becomes its neighbour and its predecessor
- Survived because: nothing asserted any of the three codes. All three share the `MEASURE` area, so a single renumbering does not merely change an identifier — it makes two codes *identical*, and a code that reaches logs and alarms silently merges two distinct failures into one.
- Verdict: KILLED by `tests/unit/test_figures.py::TestTheErrorCodesAreStable::test_each_code_renders_the_identifier_callers_match_on` and `tests/unit/test_figures.py::TestTheErrorCodesAreStable::test_no_two_codes_collide`

Two tests rather than one, and both are needed. The rendered-identifier test pins each code to the literal string a caller matches on; the collision test states the property that makes the area-plus-number scheme work at all. The second would pass on a wholesale renumbering that the first catches, and the first would pass on a new code added with a duplicate number that the second catches.

## S-12 · KILLED · `Measurement.is_empty` had the same truthiness hole as `occurred`

- Track: mechanical
- Location: src/domain/measurement/figures.py:120
- Mutants: 1 at this location
- Mutation: `@property` on `Measurement.is_empty` is removed
- Survived because: the existing `test_no_rows_at_all_is_a_legitimate_answer` asserted `assert result.is_empty` — truthy, so it passed on the bound method the mutation produces. No test asserted the *populated* case, which is the only one that can observe the difference.
- Verdict: KILLED by `tests/unit/test_figures.py::TestNullIsNeverZero::test_a_result_with_figures_does_not_claim_to_be_empty`

The third instance of one pattern in this review, after S-03 and `domain-strategy-objectives.md` S-02: a truthiness assertion passes on almost anything a broken accessor returns. This one mattered because `src/domain/measurement/answers.py:164` feeds `is_empty` into the narrative, so an always-true version tells every caller the window held nothing — including answers that carry figures. The existing assertion was strengthened to identity rather than replaced, because the empty case is still worth stating.

## S-13 · KILLED · Immutability was unasserted on all three values

- Track: mechanical
- Location: src/domain/measurement/figures.py:67, 86, 274
- Mutants: 3 across these 3 locations
- Mutation: `@dataclass(frozen=True, slots=True)` on `Figure`, `Measurement` and `Withholding` — `frozen=True` becomes `False`
- Survived because: no test attempted to mutate any of the three. A figure is a measurement of something that already happened, so nothing may edit one after the fact, and that guarantee was carried by the decorator alone.
- Verdict: KILLED by `tests/unit/test_figures.py::TestTheseValuesAreFrozen::test_a_figure_cannot_be_rewritten`, `::test_a_measurement_cannot_be_rewritten` and `::test_a_withholding_cannot_be_rewritten`

Same gap as `domain-strategy-objectives.md` S-04, which is now the second review to find it — worth treating as a default check on any frozen dataclass rather than something a mutation run has to rediscover per module.

## S-14 · EQUIVALENT · `slots=True` on a dataclass

- Track: mechanical
- Location: src/domain/measurement/figures.py:67, 86, 274
- Mutants: 3 across these 3 locations
- Mutation: `@dataclass(frozen=True, slots=True)` — `slots=True` becomes `False`
- Survives because: `slots` changes how attributes are stored, not what any of them are or what operations are permitted. It is the second of the two classes ADR-013 names. Separated from S-13 rather than grouped with it because the verdicts differ, even though the location and operator are shared.
- Verdict: EQUIVALENT

## Not yet triaged

The remaining 22 open survivors, by location. Each needs individual triage; none is claimed equivalent. Recorded so the next pass starts from an inventory rather than a rerun.

| Line | n | Operators, and what is unasserted |
|---|---|---|
| 144 | 3 | `ReplaceAndWithOr`, `Is_GtE`, `Is_LtE` on the currency-units guard |
| 270, 396, 405 | 2, 2, 1 | `ExceptionReplacer` on `except` clauses, two of which are `# pragma: no cover` and so arguably unreachable by design |
| 393 | 2 | ordering substitutions on the `UnitKind.COUNT` branch |
| 99, 465 | 2, 1 | `NumberReplacer` on `undefined_rows: int = 0` and a currency index |
| 215, 219, 220, 254, 269, 440, 459, 507, 271 | 1 each | assorted comparison and boolean substitutions. Several are likely equivalent for the `StrEnum` ordering reason given under S-08, but none has been checked |
