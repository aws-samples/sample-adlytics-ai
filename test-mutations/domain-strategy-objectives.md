# Mutation review — src/domain/strategy/objectives.py
> **Module retired 2026-09-07.** `src/domain/strategy/objectives.py` no longer exists; this review is retained as history.

- Commit: bfb342dc8660e78e27fd52df3929da2612621025
- Date: 2026-08-18
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/strategy/objectives.py TESTS=tests/unit/test_objectives.py`
- Mutants: 102 generated, 70 killed, 32 survived
- Survivors: 0 open, 5 killed since, 3 equivalent

First review under ADR-013. The initial run left 39 survivors, of which 4 were genuine and 1 more was found by the semantic pass. All 5 are now killed by named tests; the module's tests went from 19 to 27. Every remaining survivor belongs to one of the three equivalent classes below.

## S-01 · KILLED · The notes cap tests moved with the constant they claimed to pin

- Track: mechanical
- Location: src/domain/strategy/objectives.py:18
- Mutants: 2 at this location
- Mutation: `NOTES_MAX_CHARS = 2000` becomes `2001` / `1999`
- Survived because: both cap tests derived their input from the constant — `"x" * (NOTES_MAX_CHARS + 1)` and `"x" * NOTES_MAX_CHARS`. Mutating the constant moved the inputs with it, so no assertion could observe the change. The tests verified internal consistency, not the 2000-character contract that guarantees notes fit in a prompt.
- Verdict: KILLED by `tests/unit/test_objectives.py::TestObjectiveSetValidity::test_the_cap_is_2000_characters`

The two boundary tests now use literals 2000 and 2001, and the constant is asserted separately. Both halves are needed: literals alone would not catch the constant drifting, and the constant assertion alone would not catch the comparison operator flipping.

## S-02 · KILLED · Removing `@property` left every assertion passing

- Track: mechanical
- Location: src/domain/strategy/objectives.py:74
- Mutants: 1 at this location
- Mutation: `@property` on `Objective.primary_metric` is removed
- Survived because: without the decorator, `obj.primary_metric` evaluated to a bound method rather than a string. `test_every_objective_declares_one` asserted only truthiness, and a bound method is truthy.
- Verdict: KILLED by `tests/unit/test_objectives.py::TestPrimaryMetric::test_every_objective_declares_one`

The test now asserts `isinstance(metric, str)` before truthiness. This is the general lesson from this review: a truthiness assertion passes on almost anything, including the objects a broken accessor returns.

## S-03 · KILLED · The `binding` default was unasserted

- Track: mechanical
- Location: src/domain/strategy/objectives.py:71
- Mutants: 1 at this location
- Mutation: `binding: bool = False` becomes `binding: bool = True`
- Survived because: no test constructed an `Objective` without passing `binding` and asserted the default. Per ADR-011 the flag is modelled but unused in V1, so a flipped default changed nothing at the time — and would have become live the moment enforcement was implemented.
- Verdict: KILLED by `tests/unit/test_objectives.py::TestObjectiveDefaults::test_binding_defaults_to_advisory`

## S-04 · KILLED · Immutability was never asserted

- Track: mechanical
- Location: src/domain/strategy/objectives.py:66, 80, 99
- Mutants: 3 across these locations
- Mutation: `frozen=True` becomes `frozen=False` on `Objective`, `Assignment` and `ObjectiveSet`
- Survived because: no test attempted a mutation and expected it to raise. Frozen dataclasses are required by the hexagonal boundary rules, and nothing verified the requirement held.
- Verdict: KILLED by `tests/unit/test_objectives.py::TestImmutability::test_objective_is_frozen`, `::test_assignment_is_frozen`, `::test_objective_set_is_frozen`

Grouped across three locations by exception to the grouping rule: it was one missing test pattern, not three findings.

## S-05 · KILLED · `PRIMARY_METRIC` mis-mapping was invisible to both tools

- Track: semantic
- Location: src/domain/strategy/objectives.py:56-62
- Mutants: 0 generated — no operator in cosmic-ray or mutmut substitutes dictionary values
- Mutation: `ObjectiveKind.MAXIMISE_ROAS: "roas_attributed_14d"` becomes `"ntb_rate_12mo"`
- Survived because: every assertion on `primary_metric` checked truthiness or inequality against another objective, never the value itself. A wrong mapping would break the mixing warning in ADR-011 — the highest-value behaviour in this module — while the suite stayed green.
- Verdict: KILLED by `tests/unit/test_objectives.py::TestPrimaryMetric::test_each_objective_maps_to_its_own_metric`

Verified by applying the mutation to source and re-running: 3 failed, 24 passed. The two incidental kills were `::test_no_two_objectives_share_a_metric` and `::test_acquisition_is_not_judged_on_roas`. This is the mutation that justified the two-track split in ADR-013 — the mechanical track cannot reach it at all.

## S-06 · EQUIVALENT · Operator substitutions on a type annotation

- Track: mechanical
- Location: src/domain/strategy/objectives.py:130
- Mutants: 23 surviving at this location
- Mutation: the two `|` tokens in `campaign_key: str | None = None, channel_key: str | None = None` are replaced by every other binary operator, and the keyword-only `*` marker by `/`
- Survives because: annotations under `from __future__ import annotations` are stored as strings and never evaluated at runtime, so no substitution can change behaviour.
- Verdict: EQUIVALENT — known class: type annotations are inert

72 percent of the remaining survivors in this module come from this one signature line. Dismissed once here so it is not re-derived on every run.

## S-07 · EQUIVALENT · `slots=True` flips

- Track: mechanical
- Location: src/domain/strategy/objectives.py:66, 80, 99
- Mutants: 3 surviving across these locations
- Mutation: `slots=True` becomes `slots=False`
- Survives because: `slots` changes attribute storage, not behaviour. Nothing in this module or its tests depends on the absence of `__dict__`.
- Verdict: EQUIVALENT — known class: dataclass slots argument is inert

## S-08 · EQUIVALENT · Identity comparisons on enum members

- Track: mechanical
- Location: src/domain/strategy/objectives.py:93, 95, 116
- Mutants: 6 surviving across these locations
- Mutation: `is` becomes `==` or `<=`, and `is not` becomes `!=` or `>`, against `Scope.ACCOUNT`
- Survives because: `Scope` is a `StrEnum` with singleton members, so identity and equality agree. The ordering variants also agree across the three actual member values, since `"account"` sorts before both `"campaign"` and `"channel"`.
- Verdict: EQUIVALENT — enum identity equals enum equality

The ordering variants are equivalent by accident of the current member names, not by construction. Re-triage if a `Scope` member is added that sorts before `"account"`.
