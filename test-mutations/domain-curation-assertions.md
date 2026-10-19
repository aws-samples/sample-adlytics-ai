# Mutation review — src/domain/curation/assertions.py

- Commit: 82c9be0495cda8f6866203e519dba648a215f467
- Date: 2026-08-25
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/curation/assertions.py TESTS=tests/unit/test_curation_assertions.py`
- Mutants: 221 generated, 115 killed, 106 survived
- Survivors: 0 open **in the code this review covers**, 106 pre-existing and open elsewhere

Run because this branch added a fifth assertion, `check_period_overlap`. **Every mutant of the new
code was killed with no test changes** — the seven tests written alongside it already covered its
whole surface, including the boundary between adjacency and intersection and the deliberate
non-violation of an exactly equal period.

That result is the whole finding for the new code and it is not the interesting one. The module's
overall survival rate is 48%, and this is its first mutation review, so the 106 survivors are debt
that predates the branch. They are recorded here rather than fixed because the review's scope was
the new assertion, and because the largest cluster is a different concern with its own history.

## S-01 · NOT MINE · `check_dimension_resolution` has 52 survivors

- Track: pre-existing
- Location: src/domain/curation/assertions.py:472-536
- Verdict: OPEN, out of scope for this branch

Half the module's surviving mutants are in one function, and the shape of them says why: the
assertion is exercised through generated fixtures rather than through hand-built inputs, so the
sample truncation, the count arithmetic and the all-null skip are all reachable but never varied.
Its `MAX_UNRESOLVED_REPORTED` truncation in particular has no test that crosses the boundary.

Worth a review of its own. It is the assertion that decides whether a fact whose products have not
landed is quarantined, so its arithmetic decides whether a real delivery is refused — and the
`known_keys` empty case is explicitly documented as legitimate, which means the branch that
tolerates it is load-bearing.

## S-02 · NOT MINE · `_sum_of` and `detail_and_total` have 19 between them

- Track: pre-existing
- Location: src/domain/curation/assertions.py:378-390, 342-377
- Verdict: OPEN, out of scope

Both are helpers behind the control-total assertion, and both are only reached through it. The
control-total tests assert the *outcome* of a comparison rather than the sum that produced it, so an
operator flip inside the summation survives whenever the fixture's numbers happen to still
disagree — which they do, because the fixture was built to make them disagree.

## S-03 · EQUIVALENT · `AddNot` on `if TYPE_CHECKING`

- Track: equivalent
- Location: src/domain/curation/assertions.py, module scope
- Verdict: EQUIVALENT

`TYPE_CHECKING` is false at runtime by definition, so negating the guard changes which imports a
type checker sees and nothing a test can observe.

## What the new assertion's tests already covered

Recorded because it is the useful half for the next person, and because "no survivors" is only
meaningful alongside what was tried:

- Both `NumberReplacer` mutants on the period arithmetic — killed by the adjacency test, which is
  the one case where off-by-one changes the answer.
- `ReplaceAndWithOr` in the intersection predicate — killed by `DateRange.overlaps`' own symmetry
  and adjacency tests, which pin both halves of the comparison independently.
- `ReplaceNotEqWithEq` on the equality exclusion — killed by the restatement test, which asserts
  that an identical period is *not* a violation. That test exists because the exclusion is a
  deliberate carve-out rather than an optimisation, and mutating it turns every restatement into a
  quarantine.
- The `ZeroIterationForLoop` on the clash collection — killed by the test asserting that every
  overlapping period is named, not just the first.
