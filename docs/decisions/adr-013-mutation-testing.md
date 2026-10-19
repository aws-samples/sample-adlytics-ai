# ADR-013 — Mutation testing: cosmic-ray for the syntactic space, an adversarial pass for the semantic space

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-18 |
| **Decision** | **Mutation testing runs in two tracks against one shared report.** `cosmic-ray` covers the mechanical mutation space; a deliberate adversarial review covers the semantic space that no tool can generate. Surviving mutants are recorded in `test-mutations/`. **The mutation score is never a merge gate** — every survivor is triaged individually to killed or equivalent-with-a-named-reason. `mutmut` is rejected. |
| **Related** | [repo-structure](../repo-structure.md) §6 |

## Context

Coverage tells us a line executed. It does not tell us an assertion would notice if that line were wrong. The failure mode this guards against is a test that passes regardless of the code — and we already shipped one.

Mutation testing answers the real question by changing the **source** and checking whether the suite objects. A change the tests do not notice is a *surviving mutant*, and it is direct evidence of a blind spot. This direction is worth stating explicitly because the intuitive reading is backwards: the tests are not mutated, the code is. [verified — [Stryker](https://stryker-mutator.io/docs/), [Wikipedia](https://en.wikipedia.org/wiki/Mutation_testing), [CircleCI](https://circleci.com/blog/what-is-mutation-testing/), and confirmed by probe below]

The whole decision rests on a probe of `src/domain/strategy/objectives.py` — 180 lines, five validation branches, the scope-precedence rule, and 19 existing tests. Both candidate tools were run against identical source, 2026-08-18. [verified]

## The probe

**`mutmut` 3.7.0 produced two mutants for the entire file.** Both were killed, which would have reported a flawless score. A discriminating probe isolated why:

| Construct | `mutmut` 3 mutates it? |
|---|---|
| Module-level function | yes |
| Method on an undecorated class | yes |
| Method on a `@dataclass` class | **no** |
| Module-level constant or literal | **no** |

The PyPI page for 3.7.0 confirms the second half of this, recommending mutmut 2 for code outside functions. [verified]

That is disqualifying here rather than merely limited. Our domain is **20 frozen dataclasses carrying ~50 methods, against 3 module-level functions.** `mutmut` would exercise those three, skip `resolve()`, both `__post_init__` validators, `primary_metric`, `PRIMARY_METRIC` and `NOTES_MAX_CHARS`, and report green. **A green score covering a twentieth of the logic is worse than no tool at all**, because it is indistinguishable from assurance.

**`cosmic-ray` 8.7.0 produced 102 mutants on the same file, of which 39 survived.** It installs and runs on Python 3.14.6. [verified]

It also mutates **in place** and restores afterwards, where `mutmut` copies the tree into `mutants/`. An interrupted cosmic-ray run therefore leaves mutated source on disk. [verified by probe]

## What the survivors actually showed

The 39 survivors are not 39 findings, and pretending otherwise would make the metric useless.

**Twenty of them trace to one line** — `campaign_key: str | None = None, channel_key: str | None = None`. Two `|` tokens times ten binary-operator substitutions. Under `from __future__ import annotations` those annotations are never evaluated, so the mutants cannot change behaviour. They are equivalent mutants: noise, and structurally guaranteed noise wherever modern type hints appear. Several more come from flipping `slots=True`, which is equally invisible.

Roughly two-thirds of the survivors were noise. **This is why the score cannot be a gate.** A threshold over a population that is 60% inert measures type-annotation density, not test quality.

One retired module exposed the first real survivor: deriving boundary-test input from the constant
under test makes the test move with the implementation. Such tests establish internal consistency,
not the contract, and truthiness checks do not pin the boundary.

## The finding that shapes the design

**The retired module also exposed a second class of real survivor:** mutation tools do not generally
substitute dictionary values. That leaves semantic mappings and precedence rules outside their mechanical
operator space, so adversarial review remains necessary for such code.

So the two tracks are not belt-and-braces. They cover disjoint spaces, and the space the tools cannot reach is where our domain risk actually concentrates: a wrong mapping, an inverted precedence, a validation reordered so the weaker check runs first.

## Decision

**Track one, mechanical.** `cosmic-ray`, scoped per module. Operators, boundaries, constants. Reproducible and re-runnable by anyone.

**Track two, semantic.** A deliberate adversarial pass proposing mutations that are domain-plausible and tool-inexpressible. Judged by whether a named test kills them.

**One report per module in `test-mutations/`**, recording survivors only. A pass narrative is not a finding, and a report that says everything is fine is indistinguishable from a report nobody ran.

**No score threshold gates a merge.** Each survivor is triaged to *killed* by a named new or strengthened test, or *equivalent* with the reason stated. Dismissing the annotation class once, explicitly, is the point — it stops that noise being silently re-dismissed on every future run.

### Rejected

| Rejected | Why |
|---|---|
| `mutmut` | Two mutants for a 180-line file; skips every `@dataclass` method and all module-level code, which is nearly all of our domain |
| Mutation score as a merge gate | Two-thirds of survivors were inert; the number would track type-hint density |
| Tool only | Cannot express the mis-mapping, precedence-inversion or reordering mutations that carry our real risk |
| Adversarial pass only | Non-deterministic and unauditable over the mechanical space a tool covers exhaustively and cheaply |
| Reshaping the domain to suit the tool | Frozen dataclasses are required by the hexagonal boundary rules. The tool serves the design |
| Running on every commit | 102 mutants for one small module. This is a post-feature review activity, not an inner-loop check |

## Consequences

The report directory is a defect ledger with a shelf life, so every entry carries a date and the commit it was produced against. A stale report must be visibly stale rather than quietly reassuring.

Two prohibitions carry most of the value and are enforced by review, not by script: **never change source to dodge a mutant**, and **never treat regenerating the report as remediation**. Both convert the exercise into theatre while leaving the metric looking better.

The three findings above are recorded but deliberately **not fixed in this change** — `objectives.py` is on `main` and the current branch owns the auth flow. They are the first entries in `test-mutations/`.
