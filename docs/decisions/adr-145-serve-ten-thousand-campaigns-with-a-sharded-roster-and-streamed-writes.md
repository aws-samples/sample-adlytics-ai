# ADR-145: Serve 10,000 campaigns with a sharded roster and streamed writes
Date: 2026-10-18
Status: Proposed (2026-10-18). On 2026-10-18 (UTC) the owner approved:

- a Campaigns bound of 10,000, with "Showing N of M" beyond it and nothing beyond 10,000 now;
- a split name roster and streamed writes;
- baker memory raised from 1,024 to 2,048 MB, an approved `infra/` change.

When accepted, it amends ADR-071 decision 4 and its review trigger's named fix, ADR-125 decision 2
(Retail's child digests move into the sibling index), and ADR-127's per-filter roster (markers
added there).

> **Amendment, 2026-10-25 (UTC).** [ADR-152](adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md)
> decision 8 replaces the 10,000-campaign bound and "Showing N of M": neither was built, and both
> are deleted. The Campaigns population is limited only by the memory-derived `BAKE_MAX_ROWS`,
> past which the page is `population_over_ceiling`, alarmed. The sharded roster, streamed writes
> and 2,048 MB Baker stand. The Baker's memory is now the contract's `bake_tasks.baker_memory_mib`.

> **Amendment, 2026-10-27 (UTC).** Decision 4's fixed "at most 8 in flight" is replaced by a
> derived width, `bake_tasks.baker_write_width`, its one home beside `baker_memory_mib`. Why: on
> dev, Campaigns' PUTs at 8 chains filled only 68–75% of its write phase, and a probe of the
> real stage writing synthetic pairs to the dev bucket (2026-10-27, from one operator host, not
> the Lambda) found sibling throughput still rising from 32 to 64 pairs in flight with no 503
> SlowDown: Audiences-sized pairs over one client pooled at the width, the arm the committed
> probe runs, at 75, 136, 247 and 449 objects/s at 8, 16, 32 and 64 [measured, means of 3 runs;
> validation/RESULTS.md]. So the width is the smallest one whose memory ceiling's objects,
> counted structurally and its own write buffer counted, fit the 450 s write allowance at the 100 ms per-object
> budget, capped at 64, the widest width measured still rising; `make check-bakeability` refuses
> any other value. Both stores share one S3 client whose pool holds that many connections. A
> width past 64 is unmeasured; past it the lever is splitting a page's writes across invocations,
> not a wider pool. The current widths and write ceilings are the
> [scale envelope](../ui/preset-artifact-publisher.md#scale-envelope)'s.

## Context
ADR-071 made every campaign reachable through baked ordered segments and a name roster in the
parent body. The estate is bounded at 1,000 campaigns, ADR-071's falsification population for the
dev seeding tier. Above it the bake returns nothing and the page is empty.

Large sellers have 2,000–4,500 campaigns, a projection. Lifting the bound exposes five binders:

1. **The parent body is full.** It already sits 871 bytes under the 256,000-byte cap at 1,000
   campaigns, with the roster inside it.
2. **Every body is held in memory at once.** That's about 12,000 objects at 10,000 campaigns.
3. **The parent custody would overflow.** It lists every segment's digest inline, about 380,000
   bytes at 10,000 campaigns: a fatal body-ceiling failure that withholds every page.
4. **Daily series are chunked by chat's 10,000-row rule,** so the plan count grows with the
   population. Yet the page renders only per-filter daily sums, never a campaign's daily value.
5. **The whole cohort is one batch** whose raw results are all resident before any is shaped.

## Decision
1. **10,000 shown; ranking above it.**
   - The bake browses the population up to a memory-sized guard, the bake row ceiling (ADR-143),
     and measures it.
   - When the population exceeds 10,000, it shows the 10,000 with the most spend in the window,
     with the server's sentence "Showing the N campaigns with the most spend, of M." Totals and the
     trend still cover every campaign.
   - A population over the guard is the page's isolated refusal (ADR-142), never an empty body.
   - 10,000 is an owner product envelope (about twice the large-seller projection) at which every
     term fits. It isn't a ceiling, and it isn't derived from a chat rule.
2. **A constant query shape.** Compares ask for a constant width. Each daily operand is one read
   over the whole window, streamed through a reduction that keeps per-filter daily sums. Campaigns
   asks 26 questions at every preset and every population.
3. **A sharded name roster.**
   - Each filter's roster is cut into ordered sibling shards of 500 entries.
   - The parent carries the shard count.
   - The browser fetches the shards when the filter first gets focus, never on page load, and
     filters them as today. A shard from another revision reloads the parent once.
4. **Streamed writes and a sharded digest index**, the one mechanism for every source's siblings:
   - Sources stream sibling pairs, and the stage writes at most 8 in flight until the
     2026-10-27 amendment, now the derived width.
   - The stage records each pair's digests, writes them as custody-namespace index shards of 800,
     and builds the parent custody from the source's header plus the index shards' digests.
   - The pointer is completed from the staged custody digest.
   - The chain from pointer to body stays closed and digest-checked at serve.
   - Every sibling of every source is indexed, Retail's product-detail children included. Retail's
     custody keeps its served products and their references, and its children's digests live
     only in the index, so there's one digest home and one verification path.
   - The stage stops submitting writes when less than the invocation's tail time remains, and the
     page settles its designed absence (ADR-142, ADR-144). The stage and the legacy entry's
     publish both take the remaining time as a required argument, so no caller writes without
     that deadline.
5. **Memory 2,048 MB, counted whole.** The baker's memory doubles. The build gate's memory model
   (ADR-143) counts every held answer at the guard, instead of every body at once.
6. **Certified at bounds that don't grow with the seller.** Query counts are constant by shape, and
   writes are certified at 10,000 shown. The derived budgets are defect alarms (ADR-142).

Decided by rubric items (1) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a large seller's
Campaigns page was empty, and raising the bound alone would have turned that into every page going
stale.

## Will be enforced by
- `tests/unit/test_preset_artifact_publisher.py`: at most the contract's write width in flight,
  over one S3 client pooled at that width; the first failure
  stops submission; index shards cut and sorted; a tampered shard refused at serve; the runtime
  write deadline stopping submission without writing a parent, and through the legacy publish
  without moving its pointer.
- The Retail custody and product-detail tests: no inline child digests; a child found and checked
  through the index; a reference absent from the index refused.
- The Campaigns source tests:
  - the top 10,000 by spend with the sentence, paired with an estate of exactly 10,000 and no
    sentence;
  - a population over the guard refused;
  - 26 plans at every preset, and a trend equal to the chunked trend captured before the change;
  - roster shards at their edges.
- `tests/unit/test_preset_bakeability.py`: the certified table recomputed from formulas, the memory
  constant equal to the stack, and the estate test at the guard within the memory model; the
  contract's write width the one the deployed memory derives, from the structural Audiences
  object count at its edges.
- `infra/test/publisher-stacks.test.ts`: 2,048 MB.

## Consequences
- Every campaign up to 10,000 is reachable as today. Above that, the most-spend 10,000 are
  reachable and the page says so.
- The filter's first focus makes 1 fetch at small estates and 20 at 10,000.
- The certified count is 26 at every preset (from 26–34). Writes at 10,000 shown take a budgeted
  153 s, so each preset's alarm budget is 300 s, inside the 900 s Lambda.
- One more custody read per segment request.
- Render formats bump, so every pointer rebuilds and cached insight slots refill once.
- Each bake second costs twice the memory price.
- Retail's product detail is verified through the index like every sibling: one more custody
  read per product-detail open, and the Retail custody format changes once.

## Alternatives considered
- **Raise the constant only.** Rejected by (4): the parent body and custody then fail fatally,
  which stales every page.
- **Chunk daily series by the bake ceiling.** Rejected: the plan count still grows with the
  population, and the page needs only sums.
- **A server-side search index.** Rejected: it adds a request-time query, against ADR-071.
- **Drop per-campaign spark lines at scale.** Not needed: rows carry none.
- **Keep Retail's children in their own inline custody set and out of the index.** Rejected: the
  same kind of object would have two custody shapes and two verification paths.

## Review trigger
A seller near 10,000 campaigns, which reopens decision 7. The next binders are measured or budgeted
terms only: the write allowance holds about 29,000 shown at the 0.1 s per-object budget, and the
population guard is the bake row ceiling. Or a second page whose siblings outgrow one custody.
