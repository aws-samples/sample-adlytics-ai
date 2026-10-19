# Mutation review — the context engine's four domain modules
> **Modules mostly retired.** Under `src/domain/conversation/`, `context.py` (2026-09-04), `strategy.py` (2026-09-07), `capability.py` and `semantics.py` (2026-09-14) no longer exist, nor does `src/domain/strategy/objectives.py` (2026-09-07). `temporal.py` still exists and has changed since this review; this review is retained as history.

- Commit: 13c443f63bdb951f2e5150a7aeee271e0d3cfe2d (reviewed), fixes on top
- Date: 2026-08-23
- Tool: cosmic-ray 8.7.0
- Modules: `context.py`, `temporal.py`, `capability.py`, `semantics.py`, `strategy.py`, plus `domain/strategy/objectives.py` which this wave changed

| Module | Mutants | Survivors before | Survivors after | Open |
|---|---|---|---|---|
| `domain/conversation/context.py` | 42 | 11 | 7 | 0 |
| `domain/conversation/temporal.py` | 113 | 14 | 3 | 0 |
| `domain/conversation/capability.py` | 107 | 24 | 8 | 0 |
| `domain/conversation/semantics.py` | 68 | 17 | 11 | 1 |
| `domain/conversation/strategy.py` | 15 | 11 | 11 | 0 |
| `domain/strategy/objectives.py` | 140 | 50 | 42 | 0 |

**Reviewed in one report rather than six, because five of the six modules are one component** and the findings transfer between them. `objectives.py` is included because this wave added `Resolution` to it and changed `resolve`'s return type, so its 2026-08-18 review no longer covers the module as it stands — that report stays valid for what it examined and this one supersedes its coverage claim.

Nineteen mutants were killed. Two of the fixes changed *source* rather than tests, and those are the two worth reading.

---

## S-01 · KILLED · The ceiling was compared with no test at the boundary

- Track: mechanical
- Location: `src/domain/conversation/context.py`, the size check in `render`
- Mutants: 1
- Mutation: `len(text) <= MAX_CONTEXT_CHARS` becomes `<`
- Survived because: every drop test derived its input from `MAX_CONTEXT_CHARS` and overshot it by a wide margin, so a comparison shifted by one changed nothing. A context of exactly the ceiling would have had a layer dropped, and the prompt would silently lose its cross-metric warnings on a deployment sitting right at the limit.
- Verdict: KILLED by `test_a_context_exactly_at_the_ceiling_is_kept_whole` and `test_one_character_over_the_ceiling_drops_a_layer`

Written as a pair. Either alone passes under a shifted comparison, and the pair needed a helper that derives the layer separator's cost from two renders rather than reading the module's private constant — so the test measures the boundary rather than restating an internal.

## S-02 · KILLED · The ceiling constant was pinned by nothing

- Track: mechanical
- Location: `src/domain/conversation/context.py`, `MAX_CONTEXT_CHARS`
- Mutants: 2 (`24_000` → `24001` / `23999`)
- Survived because: exactly the defect this repository's first mutation review recorded as S-01 on `NOTES_MAX_CHARS`, reappearing in a new module. Every test built its input from the constant, so mutating the constant moved the inputs with it.
- Verdict: KILLED by `test_the_ceiling_is_24000_characters`

**Worth noting that the lesson did not transfer on its own.** The earlier review's write-up says both halves are needed — a literal to catch the constant drifting, and boundary tests to catch the comparison flipping — and this module was written five days later without either. A finding recorded in a report is not a finding a later author applies.

## S-03 · KILLED · A drop removed layers it was not asked to drop

- Track: semantic
- Location: `src/domain/conversation/context.py`, the filter that removes a dropped layer
- Mutants: 1
- Mutation: `if entry[0] is not droppable` becomes `if entry[0] > droppable`
- Survived because: `Layer` is a `StrEnum`, so `>` compares the member *values* as strings. Dropping `semantics` then kept only the layers whose value sorts above `"semantics"` — `temporal` and `strategy` — and silently discarded the tool-contract invariant and the capability statement. Every assertion still passed: `dropped` correctly reported `(Layer.SEMANTICS,)`, the dropped body was absent, the retained body was present, and the result fitted the ceiling. Nothing looked at what else had gone.
- Verdict: KILLED by `test_a_drop_removes_only_the_named_layer`

**This is the most valuable finding in the wave, and it is the shape ADR-013 exists for.** The mutation produced a prompt with no invariant and no coverage statement — the exact state `__post_init__` was written to make unconstructible — and it did so through a code path that had four assertions pointing at it, none of which asked the question. A test that names what must *survive* a drop is a different test from one that names what was dropped.

## S-04 · KILLED · Only one trailing period's length was asserted

- Track: mechanical
- Location: `src/domain/conversation/temporal.py`, `reference_periods`
- Mutants: 5 (`14` → `13`/`15`, `30` → `29`/`31`, and `days - 1` → `days ^ 1` in `_trailing`)
- Survived because: the suite asserted that the 7-day period covered seven days and left the 14- and 30-day periods to be trusted. `7 ^ 1` is `6`, which is also `7 - 1`, so even the shared helper was invisible through the one period that was checked.
- Verdict: KILLED by `test_a_trailing_period_covers_exactly_the_days_it_names`, parametrised over 7, 14 and 30

The parametrised form also asserts each range *ends yesterday*, which is the property the whole layer exists for — a period ending today asks for a day no source has reported.

## S-05 · KILLED · The anchor every trailing period is built from was unasserted

- Track: mechanical
- Location: `src/domain/conversation/temporal.py`, `yesterday`
- Mutants: 1 (`timedelta(days=1)` → `days=2`)
- Survived because: the only assertion about the trailing periods was that they end before today, which holds just as well when every one of them is shifted a day earlier. Each period would still have had the length it claimed.
- Verdict: KILLED by `test_yesterday_is_the_day_before_today`

## S-06 · SOURCE CHANGED · `- MONDAY` was arithmetic that could not be wrong

- Track: semantic
- Location: `src/domain/conversation/temporal.py`, `this_monday`
- Mutants: 5 (`today.weekday() - MONDAY` replaced by `+`, `>>`, `<<`, `|`, `^`)
- Survived because: `MONDAY` was `0`, so every one of those operators produces the same result. Five unkillable mutants at one expression.
- Verdict: **the subtraction was removed.** `date.weekday()` is already Monday-based, so the distance back to Monday *is* the weekday. The named constant read as a deliberate offset and was decoration.

**The mutation was right and the code was wrong**, which is the outcome this technique is least often credited with. The fact `MONDAY` was documenting — that Python's weekday is Monday-based — is now pinned by `test_pythons_weekday_is_monday_based`, where it can actually fail. A named zero subtracted from something is a comment that costs five mutants and proves nothing.

## S-07 · KILLED · The stale group had no test at all

- Track: semantic
- Location: `src/domain/conversation/capability.py`, `_state_of`
- Mutants: 9 across the three status branches, including `is` → `>=`, `<=`, `!=` and `is not`
- Survived because: **every coverage fixture in the suite was `FRESH`, `QUARANTINED` or `NEVER_LOADED`.** The `STALE` branch — "loaded but behind the freshness the contract declares" — was never rendered, so routing a stale fact into the quarantine group, or into the answerable group, changed no output any assertion read.
- Verdict: KILLED by `test_a_stale_fact_is_answerable_and_says_how_old_it_is`, `test_a_stale_fact_is_not_reported_as_answerable_and_current` and `test_a_fresh_fact_appears_under_the_current_heading`

Two directions were needed and the second is the subtle one. `<=` routed *fresh* facts into the stale group, and every existing assertion passed — a window, a length and a load age render identically under either heading. Only the heading differs, and only the heading is what a reader acts on. So the fresh case needed its own positive assertion about which group it lands in.

## S-08 · KILLED · The load-age bands were sampled in the middle

- Track: mechanical
- Location: `src/domain/conversation/capability.py`, `_loaded_phrase`
- Mutants: 6 (`hours < 1` shifted and relaxed, `hours < 48` shifted and relaxed, `hours // 24` shifted)
- Survived because: the two tests used 5 hours and 73 hours — comfortably inside their bands, so any shift of a band edge kept them in the same band. The sub-hour case had no test at all.
- Verdict: KILLED by `test_the_load_age_is_reported_at_every_boundary`, parametrised over 30 minutes, 1 hour, 47 hours, 48 hours, 69 hours and 3 days

How old the data is, is what a staleness caveat is written from, so a wrong age is a wrong caveat. 69 hours is in the list because `// 24` mutated to `// 23` agrees with the truth at 72 hours and disagrees at 69.

## S-09 · KILLED · Two warnings fired on everything and the tests only checked they fired

- Track: semantic
- Location: `src/domain/conversation/semantics.py`, the system-count guard and the proper-subset test
- Mutants: 5 (`<` → `>` on the system count; `<` → `<=`, `is not`, and `and` → `or` on the grain comparison)
- Survived because: every assertion named a metric that **must** appear. Relaxing a comparison so that *every* metric appears satisfies all of them. The `>` mutation produced a section announcing that two measurement systems both report `ad_attributed`, which comes from AMC alone; the `<=` mutation listed every ratio in the dictionary as narrowed, including the eighteen that are not.
- Verdict: KILLED by `test_a_basis_reported_by_one_system_gets_no_warning` and `test_a_ratio_as_wide_as_its_operands_is_not_named`

**A warning that fires on everything says nothing**, and a suite that only asserts presence cannot tell the two apart. Both new tests derive their expected *absences* from the dictionary rather than naming metrics, so they keep working as it changes.

## S-10 · SOURCE CHANGED · A `continue` behaved as a guard only because of file ordering

- Track: semantic
- Location: `src/domain/conversation/semantics.py`, the two-basis skip
- Mutants: 1 (`continue` → `break`)
- Survived because: `break` stopped collecting at the first metric declaring two bases, so every metric declared after `roas_blended` in the dictionary vanished from the warning. The suite passed because the metrics it named happened to be declared earlier in the file.
- Verdict: **the `continue` was replaced by a filter in a comprehension**, so there is no branch to get wrong — and KILLED additionally by `test_every_single_basis_metric_with_a_shared_denomination_is_reached`, which asserts the whole expected set rather than examples.

A test that depends on the ordering of a YAML file is a test that passes for a reason nobody chose. The completeness assertion is what makes the collection the property under test.

## S-11 · KILLED · `Resolution`'s tests were in another module's file

- Track: process
- Location: `src/domain/strategy/objectives.py`, `Resolution.__post_init__` and `channel_assignments`
- Mutants: 8, including `is` → `is not` on the channel filter and `@property` removed
- Survived because: **both symbols were added to `objectives.py` and tested in `tests/unit/test_strategy_layer.py`.** `make mutate` runs one module against one test file, so the module's own review loaded a suite that did not cover its newest code. `is not` on the channel filter returned the account and campaign assignments *as channels*, and nothing in `test_objectives.py` looked.
- Verdict: KILLED by moving `TestResolutionReportsItsScope` into `tests/unit/test_objectives.py` and adding `TestChannelAssignments`

**The finding is about where a test lives, not what it asserts.** A test in the wrong file still passes, still covers the line, and is invisible to the one technique that would have found the defect. The rule this suggests: a symbol's tests belong in the file named after the module that declares it, and mutation review is what enforces that rather than review.

`@property` removal is the same shape as this module's own S-02 from 2026-08-18 — a bound method is truthy and passes a loose assertion — so `test_it_is_a_property_rather_than_a_method` asserts `isinstance(..., tuple)`.

---

## Equivalent classes, and one open survivor

Every remaining survivor falls into one of these. None is a defect and none should be "fixed".

**`|` in a union annotation.** 34 of the 42 survivors on `objectives.py` and all 11 on `strategy.py`. `from __future__ import annotations` never evaluates an annotation, so no operator substitution in one can change behaviour. Already recorded by the `domain/measurement/figures` review, where it was 484 of 590.

**`is` against a `StrEnum` member, replaced by `==` or by an ordering that happens to agree.** The largest class here. `Scope`, `FreshnessStatus`, `MetricKind` and the private `_State` are all `StrEnum`, so `==` is genuinely equivalent to `is`, and an ordering comparison frequently agrees over the two or three values a branch can actually see — `"account" <= "account"` is true while `"campaign"` and `"channel"` are both false, which reproduces the identity test exactly. Killing these would mean asserting on values the type system already excludes.

**`frozen=True` and `slots=True` on a dataclass.** `frozen` is asserted for every dataclass in every domain module by `tests/unit/test_domain_invariants.py`, which a module-scoped run does not load — so it survives here and is killed there. `slots` is one of ADR-013's named equivalent classes.

**`int` identity comparisons.** `len(x) is not 2` and similar. CPython interns small integers, so identity and equality agree — the third shape `domain/curation/watermarks` recorded, arriving through a length check rather than a checksum.

**Floor division replaced by true division inside `int(...)`.** `int(x // 3600)` and `int(x / 3600)` agree for every non-negative duration, because `int` truncates.

**Arithmetic that is equivalent at the values in play.** `DAYS_IN_WEEK - 1` and `DAYS_IN_WEEK ^ 1` are both 6, because 7 is `0b111`. `3600` mutated to `3599` diverges only for a duration within one second of a multiple of 3599, which no realistic load age hits.

**A `TYPE_CHECKING` guard with `not` added.** The guarded imports are valid at runtime, so executing them changes import timing and nothing else.

### The one open survivor

**`domain/conversation/semantics.py`, `_shared_denominations`: `continue` → `break` on the single-system skip.** Equivalent *under the current dictionary* and not equivalent in principle. Unit kinds are iterated in sorted order — `count`, `currency`, `ratio` — and the only kind reported by one system is `ratio`, which sorts last, so breaking there produces identical output to skipping it. A dictionary in which a single-system unit kind sorted before a shared one would silently lose the shared one.

Left open deliberately rather than closed with a fabricated catalogue. This module's whole value is that it is derived from the *real* dictionary, and a test asserting against a synthetic one would prove the projection runs — which is what the module docstring says a fixture cannot establish. The honest options are to iterate in an order that cannot hide the case, or to accept it and re-check when a unit kind is added. Recorded here so the next reader does not spend the triage again.

## Two lessons for the next review

**Assert the absences, not only the presences.** Five of the eleven findings above are one shape: a comparison relaxed so that everything passes the filter, under a suite that only ever asked whether the right thing was included. `_State` routing, both semantics guards, and the ratio-narrowing test all failed this way.

**A test's file is part of its coverage.** S-11 is not about an assertion being weak; it is about a correct assertion being in a file the module's own review does not load. Nothing but mutation would have shown it, and the next module split should move its tests with it.
