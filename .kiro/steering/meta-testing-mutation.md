---
inclusion: manual
description: Governs deliberate post-implementation mutation reviews, mechanical and semantic mutation tracks, survivor remediation, and reporting after a feature's tests are written.
---

# Mutation review

Decision, probe evidence and rejected alternatives: docs/decisions/adr-013-mutation-testing.md.

Mutate the source, never the tests. A test suite that does not object to a changed
source line has a blind spot, and that blind spot is the finding.

## Before running

- Run the full suite first and confirm it is green. A red baseline makes every
  mutant look killed.
- Commit or stash everything first. cosmic-ray mutates files in place and
  restores them afterwards, so an interrupted run leaves mutated source on disk.
- If a run is interrupted, restore the source before doing anything else. Never
  inspect or commit a working tree that a mutation run has touched.
- Scope to one module per run. Never point a run at src/ as a whole.

## Which track owns which mutation

Give cosmic-ray the mechanical space and never hand-write mutations it already
generates: comparison and arithmetic operator substitution, boundary shifts on
numeric literals, boolean literal flips, removed decorators, emptied loops.

Reserve the semantic pass for mutations no operator can express. At minimum,
consider each of these against the module under review:

- A lookup table mapping the right key to the wrong value, where both values are
  valid members of the target type.
- Two branches of a precedence chain swapped, so a broader scope wins over a
  narrower one.
- Validation checks reordered so a weaker check short-circuits before a stronger
  one, or a raise replaced with a silent return of a plausible default.
- An enum member substituted for a sibling member.
- A unit, grain or currency substituted for another valid one.

Never propose a semantic mutation that changes a signature or fails to import.
It must be a plausible thing a careful engineer could have written.

## Scope and tools

- Python is mutated with cosmic-ray, one module per run. TypeScript (frontend/, infra/) is
  mutated with StrykerJS, one file group per run. The mechanical/semantic split above applies to
  both languages.
- Mutation runs only when the owner asks for one. Never add a mutation run to make verify, a
  hook, `make publication-gate`, `make release-gate` or any automated gate.
- `make mutate-next` runs the highest-priority unit whose coverage-map entry is missing or stale.
  mutation-priorities.yaml owns the order. Never pick units by hand when the owner asks for "the
  next batch".
- Never re-run a unit whose coverage-map entry is current. An entry goes stale only when the
  fingerprint docs/test-run-records.md defines changes.

## Run records and the coverage map

- Every mutation run writes a new run record through the run recorder. docs/test-run-records.md
  owns the layout and metadata. Run records are gitignored and never edited after they are
  written.
- The local coverage map holds one current entry per unit: the fingerprint, the run record that
  produced it, and the survivor verdicts. A verdict is OPEN, KILLED (naming the test ID that
  kills it) or EQUIVALENT (naming the reason class).
- Never mark a survivor EQUIVALENT because reproducing it is inconvenient.
- The semantic track is run like the mechanical one. Every semantic mutation is applied and
  executed, and its result is recorded in the same run record.
- The mutation score is never a threshold: it measures annotation density, not test quality
  (ADR-013). `make release-gate` reports the coverage map (current, stale, missing, OPEN) and
  never blocks on it.

## Resolving a survivor

- Strengthen or add a test. Never edit the source so the mutation can no longer
  be applied.
- Never delete a survivor entry to resolve it. Change its verdict in place.
- Never treat regenerating the report as remediation.
- When a test derives its input from the constant it is checking, that test can
  never detect a change to that constant. Pin the boundary to a literal, and
  assert the constant separately against the requirement that fixed it.
