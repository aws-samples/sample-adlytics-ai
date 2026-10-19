# Mutation review — src/adapters/jobs/reset_load_state/reset.py
> **Module retired 2026-08-26.** `src/adapters/jobs/reset_load_state/reset.py` no longer exists; this review is retained as history.

- Commit: 82c9be0495cda8f6866203e519dba648a215f467
- Date: 2026-08-25
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/jobs/reset_load_state/reset.py TESTS=tests/unit/test_reset_load_state.py`
- Mutants: 57 generated, 41 killed then 45 killed, 12 survived
- Survivors: 0 open in this branch's code, 4 killed since, 5 pre-existing and open, 3 equivalent

Run because this branch extended the reset planner with raw-zone removal, residual tables and a
reach-beyond-scope report. The four survivors in the changed code all sat on one property, and the
reason they survived turned out to be more interesting than the mutants: **nothing called it.**

## S-01 · KILLED · `is_empty` had no caller, so no mutation of it could fail

- Track: semantic
- Location: src/adapters/jobs/reset_load_state/reset.py:119-122
- Mutants: 4 at this location
- Mutation: `ReplaceAndWithOr` on the conjunction, and three `ReplaceUnaryOperator_Delete_Not`
  removing each negation
- Survived because: `is_empty` was referenced only by tests. Nothing in `src/` read it, so no
  behaviour depended on the answer and every spelling of it was as correct as any other. The tests
  asserted `is False` on a populated plan, which every mutant also satisfies.
- Verdict: KILLED by `test_a_scope_that_resolves_to_nothing_is_empty`,
  `test_contracts_alone_are_enough_to_make_a_plan_non_empty`, `test_tables_alone_are_enough_too` and
  the existing `test_a_plan_that_only_removes_landed_objects_is_not_empty` — and by giving the
  property a caller.

**The fix was not more tests, it was a use.** `main()` now refuses a plan that resolves to nothing
and says so. Without that, a scope matching no contract printed an empty list and then "forgot 0
attempt(s); emptied 0 table(s); removed 0 landed object(s)", which reads as a reset that worked.

Worth recording as a pattern rather than an incident: a property with no production caller cannot be
mutation-tested meaningfully, and the survivors are the signal that it is decorative. One of this
branch's own test docstrings claimed `is_empty` "gates the whole job" while it gated nothing, which
is exactly the kind of comment that outlives the code it describes.

## S-02 · NOT MINE · `forgets_everything` has five survivors and no caller either

- Track: pre-existing
- Location: src/adapters/jobs/reset_load_state/reset.py:114-117
- Mutants: 5 — comparison operator, `and` to `or`, and two on the literal `1`
- Verdict: OPEN, out of scope

The same finding as S-01 and it predates this branch: nothing in `src/` reads it, one test asserts it
is truthy for an unscoped plan, and its `len(self.contracts) > 1` boundary is untested in either
direction. It reads like the seed of a confirmation prompt for a whole-environment reset that was
never built.

Left alone deliberately. Either it gets a caller — a confirmation on a plan that forgets everything
is a reasonable thing to want, now that `--raw` makes a full reset destructive in a way a reload
cannot undo — or it should go. Both are decisions rather than cleanups, and neither belongs in a
branch about a trigger.

## S-03 · EQUIVALENT · `!=` becomes `is not` on the environment guard

- Track: equivalent
- Location: src/adapters/jobs/reset_load_state/reset.py:135
- Mutants: 1
- Mutation: `request.environment != DEV_ONLY` becomes `request.environment is not DEV_ONLY`
- Verdict: EQUIVALENT in practice, and worth naming rather than dismissing

CPython interns short string literals, so `"dev" is not "dev"` is false and the guard still holds for
every input a test or a caller produces. It is only *equivalent* because of an implementation detail
of the interpreter, not because the two operators mean the same thing — a string built at runtime
rather than parsed as a literal would not be interned, and the guard would then refuse `dev`.

Not chased, because the value reaching it comes from `--env` on a command line, which CPython does
not intern. That makes this a mutant that is harmless here and would be a real defect one refactor
away, which is the most useful thing a review can say about it.

## S-04 · EQUIVALENT · Dataclass decorator arguments and `if TYPE_CHECKING`

- Track: equivalent
- Location: reset.py:15, 40, 84, 113
- Mutants: 6
- Verdict: EQUIVALENT

`frozen=True` and `slots=True` flipped to `False` on `ResetRequest` and `ResetPlan`, `@property`
removed from `forgets_everything`, and the `TYPE_CHECKING` guard negated. The first two are real
losses of immutability that no assertion in this suite observes because nothing here mutates a plan;
the third is invisible while the property has no caller; the fourth is inert by definition.
