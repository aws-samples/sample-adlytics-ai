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

## The report

One file per module under review, named for the module path with separators
replaced by hyphens and the extension dropped. src/domain/measurement/figures.py
becomes test-mutations/domain-measurement-figures.md.

Record surviving mutants only. Never record killed mutants individually, and
never write a report whose body is a statement that everything passed.

Every report carries this structure:

```markdown
# Mutation review — <module path>

- Commit: <full SHA the run was produced against>
- Date: <YYYY-MM-DD>
- Tool: cosmic-ray <version>
- Command: <the exact re-runnable command>
- Mutants: <total> generated, <n> killed, <n> survived
- Survivors: <n> open, <n> killed since, <n> equivalent

## S-01 · OPEN · <one-line summary>

- Track: mechanical | semantic
- Location: <file>:<line>
- Mutants: <n> surviving at this location
- Mutation: `<original>` becomes `<mutated>`
- Survives because: <which assertion is missing or which assertion moves with the mutant>
- Verdict: OPEN
```

Rules for the entries:

- Group survivors that share one location and one cause into a single entry with
  a count. Twenty operator substitutions on one type annotation are one finding,
  and listing them separately buries the findings that differ.
- Never group across locations, and never group an OPEN survivor with an
  EQUIVALENT one.
- Number survivors S-01 upward within a report. Never renumber an existing entry;
  a resolved one keeps its number so a reader can follow it across dates.
- Every survivor carries exactly one verdict: OPEN, KILLED, or EQUIVALENT.
- A KILLED verdict names the pytest node ID that now kills it. Never write KILLED
  without that identifier.
- An EQUIVALENT verdict states why the mutation cannot change behaviour. Type
  annotations under `from __future__ import annotations` and dataclass slots
  arguments are the two known classes; name the class rather than re-deriving it.
- Never mark a survivor EQUIVALENT because reproducing it is inconvenient.
- Update the commit SHA and date on every rerun, even when nothing changed.

## Resolving a survivor

- Strengthen or add a test. Never edit the source so the mutation can no longer
  be applied.
- Never delete a survivor entry to resolve it. Change its verdict in place.
- Never treat regenerating the report as remediation.
- When a test derives its input from the constant it is checking, that test can
  never detect a change to that constant. Pin the boundary to a literal, and
  assert the constant separately against the requirement that fixed it.

## What this is not

- The mutation score is never a merge gate and never appears in a report as a
  pass or fail. Report the counts and triage each survivor.
- Never add a mutation run to make verify, to a pre-commit hook, or to any
  automated gate that runs on every change.
