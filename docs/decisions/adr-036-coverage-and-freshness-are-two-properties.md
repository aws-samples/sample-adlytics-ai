# ADR-036: Coverage and freshness are two properties, not one

Date: 2026-08-19
Status: Accepted

Amends the combining rule introduced with ADR-026 and the coverage narrative introduced with
ADR-024. Supersedes nothing.

## Context

A live `roas_attributed_14d` question against deployed dev returned two correct figures and a
narrative containing this sentence:

> Nothing has ever been loaded for this fact, so the absence above is a gap in the platform rather
> than a gap in the business.

Both halves of that are false. There was no absence — the answer carried ROAS 0.9420 for
`campaign_key-001` and a genuine 0.0000 for `campaign_key-005` — and five loads had completed.
`metadata.freshness_status` was `NEVER_LOADED`.

Two independent defects produced it, and the repository already contained a documented statement
that each of them violated.

**The status was false.** `narrowest` combines one `Coverage` per contributing contract. A
cross-fact ROAS reads five: `amc-attributed-conversion`, `amc-reach-frequency`, `amc-dsp-clicks`,
`amc-dsp-views` and `amc-dsp-impressions`. Read from the dev registry, their windows were
2026-08-10..15, 08-10..15, 08-20..20, 08-20..20 and 08-25..25 — so `max(start)` was 08-25 and
`min(end)` was 08-15, and the intersection was empty. `narrowest` then *overwrote* the combined
status with `NEVER_LOADED`, because `Coverage.__post_init__` required a window and a
non-`NEVER_LOADED` status to be present together, and a window-less combination had nowhere else to
go. Meanwhile `FreshnessStatus.NEVER_LOADED`'s own docstring said "a measurement response can never
carry it".

**The sentence was unconditional.** `_coverage_lines` emitted it from `coverage.freshness` alone. It
never consulted whether any rows were returned, though `_narrative` had already computed exactly
that as `empty` one frame above and simply did not pass it down. The branch was also an early
return, so it suppressed the partial-coverage, stale and quarantine sentences below it.

A third observation, recorded and **not** fixed here, is the underlying reason the windows were
disjoint at all — see *Consequences*.

## Decision

**Coverage and freshness are two different properties of an answer, and only one direction between
them is an invariant.**

1. `Coverage` may carry a loaded status with `window=None`. That state means *these inputs have
   loaded, and no period can be stated for their combination*. `covers()` returns `False` for it,
   so it still fails towards an absence.
2. `Coverage` still refuses `NEVER_LOADED` **with** a window. That was always the real invariant: no
   load ran, so no load established a period, and naming one would be an invention.
3. `narrowest` no longer relabels a disjoint combination. It keeps the worst status its inputs
   actually reported. When an input genuinely never loaded, `_SEVERITY` already selects
   `NEVER_LOADED` with no help, so the branch was deleted rather than replaced.
4. `_coverage_lines` takes `empty`. It claims an absence only when there is one. With rows present
   and no observation it says the figures cannot be checked for completeness, which is what is
   actually true.

No new `FreshnessStatus` member. Coverage is already carried by the window and by a narrative
sentence, and partial coverage has always been reported that way — a fact loaded 08-10..08-12 and
asked about 08-10..08-16 reports `FRESH` with a narrower window. Adding a member would put the same
fact in two places.

The sentence "The loaded period could not be established, so completeness is unknown" already
existed in `_coverage_lines` and was **unreachable**, because the invariant forbade constructing the
state it describes. That it was written is evidence this resolution is the one the code was reaching
for.

## Consequences

- A cross-fact answer over disjoint loads now reports the real freshness and says completeness is
  unknown, instead of asserting that nothing was ever loaded.
- `NEVER_LOADED` remains reachable from a measurement answer, and its docstring now says so. What an
  answer must never do is describe an absence it does not have.
- Severity is unchanged. A stale input still makes a disjoint combination stale; removing the false
  label did not soften the rule.
- **Recorded, not fixed — G-33.** An `Observation` stores one window per contract, and the registry
  keeps only the latest by `run_at`. A contract that has loaded several disjoint windows therefore
  misreports what it covers: `amc-dsp-impressions` has rows for 08-10..15 and 08-25 in Iceberg and
  an observation naming only 08-25. That is why the five windows failed to intersect in the first
  place. This ADR makes the *reporting* honest; it does not make the stored window complete. Fixing
  it means either a set of windows per contract or a per-partition observation, and either is a
  change to the registry's shape rather than to how its contents are read.
- ADR-026's "the registry stores observations, not conclusions" is unaffected: this changes how
  observations combine, not what is stored.

## Alternatives rejected

**A new `FreshnessStatus` member such as `NO_COMMON_WINDOW`.** Faithful to the third state, and it
puts coverage into the status field where partial coverage is already carried by the window. Two
places for one fact, and every consumer would then need to handle a member that means "ask the
window instead".

**Leave `narrowest` alone and only fix the sentence.** Cheapest, and it removes the visible
contradiction. Rejected because `freshness_status` is in the envelope: a caller reading metadata
without the narrative would still be told nothing had ever loaded.

**Make the tool refuse when the combination covers nothing.** Superficially the conservative
choice. Rejected because the query returned real rows for the window asked about — refusing would
withhold a correct answer on the strength of a registry gap, which is failing towards an absence
that is not there.

**Widen each contract's window to a high-water mark.** Would have made the five windows intersect.
Rejected as the worst option available: it claims 08-10..08-25 is covered when three days in that
span loaded, converting a visible contradiction into a silent overstatement.

**Fix the observation model now (G-33).** The root cause, and it is a change to what the registry
stores rather than to how it is read. Out of scope for the defect this ADR closes, and recorded so
that the partial nature of this fix is on the record rather than discovered later.

## Review trigger

Revisit when G-33 is fixed. Once an observation can express several loaded windows per contract, the
disjoint-combination case stops being routine and becomes rare — at which point the question is
whether "loaded, covers nothing establishable" is still a state worth carrying, or whether a
complete observation makes it unreachable in practice.

Revisit sooner if a second consumer needs to branch on the difference between "never loaded" and
"loaded, no common window". Reading the window is the intended way to tell them apart; a second
consumer having to reconstruct that would be the argument for the enum member rejected above.
