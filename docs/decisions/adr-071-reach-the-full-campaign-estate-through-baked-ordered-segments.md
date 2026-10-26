# ADR-071: Reach the full campaign estate through baked ordered segments and a name roster

Date: 2026-09-17
Status: Accepted — implemented locally 2026-09-18; deployed and live-verified 2026-09-24

> **Proposed amendment, 2026-10-18.** [ADR-145](adr-145-serve-ten-thousand-campaigns-with-a-sharded-roster-and-streamed-writes.md) (Proposed) would move the name roster (decision 4) out of the parent body into ordered sibling shards fetched on the filter's first focus, write siblings as a stream under a sharded custody digest index, and replace the 1,000-campaign falsification population as the estate bound. Amended 2026-10-25 by [ADR-152](adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md) decision 8: no shown bound and no "Showing N of M"; the population is limited only by the memory-derived `BAKE_MAX_ROWS`. [ADR-141](adr-141-reach-every-audiences-campaign-through-grouped-reads-and-per-campaign-pages.md) (Proposed) would apply the ordered-sibling mechanism to Audiences. Implemented 2026-10-26 by the scale-limits change (PR #20) and live-validated on dev: where these amendments differ from this decision, they describe the code. Their acceptance is the owner's review.

> **Proposed amendment, 2026-10-07.** [ADR-127](adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md) (Proposed) would give each channel filter sibling its own segments and name roster, with segment identity `{filter_id}/{ordering}/segment-NNNN`. This decision stays in force, unchanged, until ADR-127 is accepted.

> **Implementation amendment, 2026-09-18.** The accepted segment model is now implemented
> with the canonical first segment embedded in the parent, every other ordered segment held as
> an immutable public/private sibling pair, and a closed parent-custody digest index as the sole
> route to those siblings. The pointer remains the only commit point. Publisher deployment
> generation now binds the packaged renderer and contract bytes, so a byte-affecting rendering
> change deterministically re-fires backfill. Local bulk acceptance is complete; deployment and
> live-browser acceptance remain outside this change.

## Context

The campaign portfolio artifact bounds its table at 50 rows. Above the bound, the engine ranks
the full authorized population and serves the top-N by the canonical sort; rows beyond the bound
are a typed truncation carrying the boundless total. Sort semantics split into two regimes:
at or below the bound a sort request is a complete serve-time reorder; above it a sort field
selects a different top-N cohort. Reaching a campaign outside the displayed cohort — tail
reach, with search/filter — was explicitly deferred as later work when the
population-scale decision was recorded (campaign LLD, 2026-09-15), and ADR-070's review
trigger names the slice that would reopen the trade.

That trigger has now fired as a product requirement rather than an observed population: the
owner's stated business outcome (2026-09-17) is an enterprise workspace whose estates run to
hundreds or low thousands of campaigns, where every campaign must be discoverable and sortable.
A campaign that never ranks into any top-50 is invisible even though the portfolio totals
include it; "your data is in the totals but you cannot find the row" fails the product's trust
promise. Pagination and name search are table stakes for an estate view.

The serving architecture constrains the mechanism. Preset pages are served exclusively from
artifacts baked at publish time (ADR-070); there is no live-measurement branch on a preset
path, and reintroducing one for the tail would put the slowest, most expensive query behind the
largest estates, reopen the refused-selection failure mode, and let a live tail disagree with a
baked head about the same total — two sources of truth on one screen. Meanwhile the expensive
work already happens: the baker's grouped portfolio measurement covers the full population
before top-N selection. What the bake discards today is the ordered tail.

ADR-070 decision point 6 ruled "sort variants are not baked", and its alternatives table
rejected "bake all eight sort variants" as "eightfold storage and bake work for a deterministic
reordering of identical rows". That reasoning was explicitly scoped to populations that fit
inside one artifact, where serve-time reorder is complete and baking orderings buys nothing. At
enterprise populations the premise inverts: a serve-time reorder of 50 rows cannot express a
population ranking, and the orderings are cheap post-processing of a measurement already paid
for.

## Decision

**The full authorized campaign population is baked, ordered, and reachable: the baker persists
complete ordered row segments per declared sortable column and direction, plus a compact name
roster for estate search; the two-regime sort split and the tail-reach deferral are retired.**

1. **Ordered segments at bake time.** After the grouped full-population measurement, the baker
   sorts the complete row set once per declared sortable column and direction and slices each
   ordering into segments at the artifact row bound. Segments are bodies in the existing
   artifact store, keyed under the parent artifact's revision; the parent artifact embeds the
   first segment of the canonical sort, so the landing render needs no extra fetch. Every
   segment of every ordering derives from the same measurement and carries the same revision —
   pagination can never mix two publication states.
2. **Truncation becomes pagination state.** The truncation record widens to a pagination
   record: position (rows m–n), segment size, segment count, and the boundless population
   total. "Showing 1–50 of 1,024" replaces the dead-end truncation banner. A population at or
   under the bound is the degenerate single-segment case; no separate regime remains.
3. **One sort semantics.** A sort request selects a baked complete ordering, never a cohort.
   The regime distinction (complete reorder below the bound, cohort selection above it) is
   retired, and with it the interim rule that above-bound sort requests return the
   canonical-sort cohort. Changing sort resets the position to the first segment. The
   sortable-column roster is artifact-declared and fixed at publish; an undeclared sort field
   remains a refused input.
4. **Name roster for estate search.** The baker persists one compact roster per (page,
   variant, revision): governed display label, channel and marketplace display labels, and the
   row position in the canonical ordering — display projections only, no canonical keys, no
   measures. The browser filters this roster client-side as the user types; choosing a match
   requests the segment containing that position through the normal segment path. The roster
   is a display index, never an authority surface: selection capabilities are minted only onto
   served segment rows, exactly as they are minted onto the served artifact today.
5. **Serve path shape is unchanged in kind.** A segment request is pointer read → segment
   fetch → capability minting on the served rows, under the same authorizer and custody rules
   as the parent artifact; it performs no measurement. Portfolio totals, trend, and insight
   slots stay population-wide on the parent artifact and do not vary by segment.
6. **Bake cost is bounded and stated.** Sorting and slicing are in-memory post-processing of
   the already-executed grouped measurement; added bake work is O(sort keys × population log
   population) and added storage is (sort keys × directions × segment count) small JSON
   bodies. The campaign-detail bake fan-out — the actual scale cost of a large population — is
   unchanged by this decision and remains governed by the bake envelope and ADR-070's named
   per-subject decomposition.

## Consequences

Every campaign in an estate is reachable: by any declared sort ordering, walked page by page,
or directly by name through the roster. The totals a user checks against the table are the
same population the table can now actually enumerate. The artifact-serving posture survives
intact — offline bodies, forward-only pointers, revision-consistent reads, capability custody
at serve time — and the biggest estates get the cheapest serving, not the most expensive.

The bake envelope arithmetic in the campaign operational design gains segment and roster
terms, and the build-time bake-ability check must cover them. The frontend gains pagination
controls and a filter input; both render served state and never rank, slice, or derive
locally. The 1,000-campaign seeded scale tier (dev seeding strategy) is the falsification
population for all of it: grouped-measurement correctness, complete orderings, segment
integrity, roster reach, and measured bake duration against the declared envelope.

ADR-070 decision point 6 and its "bake all eight sort variants" rejection are superseded in
scope: orderings are baked precisely because serve-time reorder cannot span a population. The
per-pack snapshot arithmetic, preset bounds, and everything else in ADR-070 are untouched.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Live query path for the tail (paginate via Athena at request time) | Seconds of latency on a click; per-click cost scaling with estate size; a live tail can disagree with the baked head after a restatement — two sources of truth on one page; reopens the refused-selection failure ADR-070 retired; punches a live-measurement hole in the custody posture. |
| Ship the whole population in one artifact and paginate client-side | Payload unbounded by contract — fine at 1,000 rows, broken at 10,000; violates the browser-never-ranks guardrail the moment the client sorts; the row bound exists to keep served bodies bounded. |
| Keep top-N only, add search that answers from the engine on demand | Search becomes the live tail path with all its costs; a found campaign still is not situated in any ordering the user can continue scrolling. |
| Bake only the canonical ordering and reorder segments at serve time | A segment is 50 rows of a population-wide ordering; reordering it server-side yields a sorted page of an unsorted whole — exactly the "silently resorted subset presented as a population ranking" the population-scale decision forbids. |

## Review trigger

- The declared sortable-column roster grows enough that segment fan-out (columns × directions
  × segments) materially moves the bake envelope — revisit which orderings earn baking.
- A population large enough that the name roster itself exceeds a bounded payload — the named
  next step is a baked sharded roster or a server-side search index, never a live query.
- A second consuming page needs tail reach — generalize the segment/roster mechanics into the
  shared preset-artifact-publisher concept exactly as the store itself was generalized.
