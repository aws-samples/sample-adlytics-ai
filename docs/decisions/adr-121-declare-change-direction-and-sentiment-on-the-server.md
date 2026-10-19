# ADR-121: Declare change direction and sentiment on the server
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design, applying the owner's decision of
2026-10-05 that the server declares change semantics. The owner reviewed and approved the mockups on
2026-10-07. The workflow orchestrator approved the design the same day under the owner's delegated
overnight authorization. Not implemented. The chip that renders it is in
[ADR-118](adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md).

> **Amended 2026-10-07 (Slice 3 implementation review), decisions 4 and 7 refined.**
> Decision 4: a rate a page shows rescaled rather than as a percentage changes in percent, not
> points. Retail conversion, shown as units per 1,000 glance views, is the one case, and the
> approved mockup reads it that way
> ([Retail Health LLD](../ui/retail-health/low-level-design.md)).
> Decision 7: the one-release lead applies to retained baked bodies. A Campaigns format-6 or
> Retail Health format-4 body is served in its baked form and reads "Change unavailable" until
> its pointer moves; each page's LLD owns the condition that retires that decode. The metric
> focus is a serve-time response with nothing retained, so `MetricFocus.v4` replaces v3 in one
> release ([metric focus contract](../ui/metric-focus-contract.md)). A bundle left open across
> the API deploy has strict decoders, so its focus reads as unavailable, and its Campaigns and
> Retail Health pages refuse a format-7 or format-5 body once the pointer moves, until a reload.
> That gap follows from strict decoders and server-first ordering, and the slice accepts it.
> Because baked bodies carry the tone, a `favourable_direction` change bumps both preset render
> formats ([preset artifact publisher](../ui/preset-artifact-publisher.md)).
> **Amended 2026-10-09 (owner revision of 2026-10-08, simplified delivery), decision 7.** The
> retained decode is retired: the API, a full re-bake and the browser deploy together, so no
> retained body is read. Both sides refuse a comparison without the `change` key (Campaigns
> format 6, Retail Health format 4), and a two-sided comparison with `change: null`.

## Context

The UX review of dev (2026-10-05) found change chips that show a meaningless percentage after a loss or
a near-zero prior, and tone inferred from the sign, so a fall in cost per click read as bad. The owner
decided on 2026-10-05 that the server declares direction, a "not meaningful" flag and the unit.
Nothing in the metric dictionary says which direction is good: there's no `favourable`, `polarity` or
`higher_is_better` field under `src/` or `contracts/` (searched 2026-10-06). The browser never
computes a delta [repo: `docs/ui/README.md:118`].

## Decision

**Every comparison carries a server-built `change` block, and the metric dictionary declares which
direction is favourable for every compared metric.**

1. **Contract.** `change: { direction: up | down | flat, sentiment: favourable | unfavourable |
   neutral, unit: percent | percentage_points | absolute, display_text, not_meaningful: null |
   near_zero_prior | negative_prior | no_prior }`.
2. **Governed direction.** A new required metric-dictionary field, `favourable_direction: up | down |
   none`, on every metric served with a comparison. Declaration loading fails when a compared metric
   lacks it. Spend metrics are `none`; TACoS, CPC and CPM are `down`; CTR is `up`.
3. **Sentiment.** `favourable` when `direction` equals the field, `unfavourable` when it's the
   opposite, and `neutral` when the field is `none` or the direction is `flat`.
4. **"No change" means equal unrounded values**, or within a tolerance the metric declares. It never
   means the change rounds to zero. The server computes every change from unrounded operands, and its
   text has enough precision that a real change never reads as none: a rate under 1%, such as CTR,
   shows three decimals with its change in points at three decimals, and a change smaller than the
   display step reads "<0.1%" or "<0.001 pts" with its direction.
5. **Word states.** A `not_meaningful` change renders a word state, "Up from a loss" or "New", or an
   absolute change, never a percentage.
6. **Rendering.** The chip shows the direction icon and word, a tone from `sentiment`, and
   `display_text`. The browser never infers direction or tone from a sign.
7. **Versioning.** A presentation render-format bump. The server ships the block first; the frontend
   requires it a release later.

## Consequences

- Every compared metric in the dictionary needs a reviewed `favourable_direction` before the slice
  ships.
- A change chip can't disagree with its tone, and spend never reads as good or bad.
- Two CTR falls that rounded to "No change" in an earlier mockup round now read −0.002 and −0.005
  points [fictional].

## Alternatives considered

- **Tone from the sign in the browser.** Wrong for cost metrics, and the browser would interpret
  analytics.
- **Direction only, no sentiment.** Leaves the tone without a governed source.

## Review trigger

Reopen when a metric's favourable direction depends on context, such as a goal the customer sets.
