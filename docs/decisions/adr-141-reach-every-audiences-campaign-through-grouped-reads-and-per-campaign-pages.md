# ADR-141: Reach every Audiences campaign through grouped reads and per-campaign pages
Date: 2026-10-18
Status: Proposed (2026-10-18). The owner approved the direction on 2026-10-18 (UTC): grouped
queries, per-campaign bodies fetched on selection, a paged profile, and the 7- and 11-campaign caps
deleted. Acceptance is the owner's review of this file after implementation and live acceptance.
When accepted, it supersedes the Audiences campaign and body bounds (owner decision 2026-10-14,
integration gates IG-002 and IG-003) and the dev-only 7-campaign stopgap of
2026-10-17.

> **Amendment, 2026-10-25 (UTC), scale-limits r2a ([ADR-152](adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md) decision 7).**
> As built, the three campaign figures and the prior-month segments are group-streamed, one
> campaign at a time, rather than held or reduced to keys. These reads, and the grouped history
> walk, have no row `LIMIT` (`RowCeiling.BAKE_GROUPED`); the walk keeps only the campaigns it
> grades. That changes three points below:
> - Decision 2: a cut grouped read no longer refuses the campaign section; that section's
>   `rows_over_ceiling` and its copy are deleted. The page refuses `rows_over_ceiling`, with the
>   raise-memory action, only when one held term passes the derived `BAKE_MAX_ROWS`: the campaign
>   index, its segment set, or one campaign's segments.
> - Decision 4: there is no static body bound. The stage's write deadline is the one write limit
>   (`bodies_over_invocation`), and writes are certified at the whole write allowance (450 s).
> - Consequences: 24 statements; the derived budget is 600 s. As of 2026-10-25 they run in two phases (`src/adapters/jobs/preset_artifact_publisher/audiences_source.py`); the three phases (7, 16, 1) first written here are superseded.

## Context
Audiences answers, for one DSP campaign at a time, which audience segments perform. Today the page
shows at most 7 DSP campaigns ("Showing 7 of N"). Two bounds produce that number, and neither is a
business rule:

- **The query shape.** The bake asks three metrics and up to three roster reads per campaign, so
  its distinct requests are 19 + 6 per campaign.
- **The fixed budget.** It certifies that count against a fixed 300 s task budget at a modelled
  30 s per query wave, plus a width rule. 7 is the largest count that fits.

An earlier runtime used 11. On dev it asked for query 62 against a certified 61 and failed, which
withheld every page (run `931a7065`, 2026-10-17).

The model is far from what runs. The same 7-campaign bake ran in 11.6 s against a modelled 286.6 s
(cohort `bc6dcc11`, 2026-10-18). The longest per-query engine time across 28 dev runs on 2026-10-17
was 16.2 s. The problem is the shape, not the volume.

Separately, one body carries every profile row and every campaign-by-segment row, at most 300. Past
that, the whole page shows a page-level absence, profile included. Sellers have 5–20
DSP campaigns (a small brand) to 50–200 (a large multi-brand vendor), with 20–150 segments each.
Those counts are assumptions, not catalog facts. Either bound hides campaigns or blanks the page at
both sizes.

Two facts constrain a grouped read. Campaign is already a grain of the measurement, and the
listing already plans it with an empty roster. But the segment metrics can't be aggregated across
campaign or segment: segment reach is a distinct count, and the others declare both as exact axes,
so the planner refuses a read that leaves either ungrouped.

## Decision
1. **Ask once, grouped by campaign and segment.**
   - The prior-month segment roster and the older-history check are each one statement over every
     campaign at the campaign-and-segment grain, the grain the planner accepts. They're read as
     streamed reductions that keep only keys: each campaign's segments, and the set of campaigns
     observed. Their memory is the keys, not the rows.
   - The three campaign metrics are each one statement at the same grain. The campaign list is the
     impressions answer.
   - Each campaign is graded against its own roster in the adapter, with the same rule every
     answer uses.
   - Per marketplace, the bake issues at most 24 distinct statements in two barriers, at any
     campaign count.
2. **Each campaign's and each profile path's own history gap refuses only that item.** The roster
   rules are unchanged (first month, gap, withheld month), evaluated per campaign over the grouped
   reads. A truncated grouped read never counts as "not observed": it refuses the campaign section,
   and the profile still renders.
3. **Bake a parent and paged siblings.**
   - The parent carries the envelope, page 0 of each profile path, and the first shard of a
     campaign index (label, opaque reference, segment and page counts, state).
   - Each campaign's segments are sibling bodies of 50 rows, ordered by impressions on the server.
     Each profile path's later pages and later index shards are siblings too.
   - Every sibling carries its parent's revision and is bound by digest through the parent
     custody's sharded index (ADR-145).
4. **Bound the bodies by the invocation, not by the seller.** The bake counts its bodies before
   staging. Past a bound derived from the half of an invocation reserved for writes (18,000 at
   today's 0.1 s per-object write budget and 8 write chains), the campaign section shows its own
   absence, alarmed, and the profile still publishes. Writes are certified at that bound. Because
   the bound rests on a budgeted write time, the stage also stops writing once less than the
   invocation's tail time remains; then the page keeps its last good version and alarms (ADR-142,
   ADR-145), instead of the invocation timing out.
   - The campaign rosters' grouped reads keep only keys, and each enforces the held row ceiling on
     what it keeps (ADR-143). More keys than that is the campaign section's absence, never a
     larger memory footprint.
5. **Fetch on selection, never in bulk.**
   - The existing Audiences route accepts at most one of: a campaign page, a profile page or an
     index shard, by opaque reference and page number. It exists only if the parent custody's index
     lists it.
   - The browser fetches a campaign's page only when the user selects it, prefetches at most the
     next page of that campaign, and never fetches other campaigns. A part from another revision
     reloads the parent once.
6. **No cap, one count model.**
   - The campaign bound, its certifier, the timeout estimator, the fixed 300 s budget, the width
     rule, the one-body row budget and the page-level over-bound body are deleted.
   - The page-level gate takes Audiences' count from its roster certificate, so one model counts
     the bake.
   - The budget is re-derived by the certifier's existing rule at the body bound. It's a defect
     alarm (ADR-142), never a limit on what the user sees.

Decided by rubric items (1) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): the page showed too
little, or nothing, at every real size, and the old shape failed the whole cohort when it grew.

## Will be enforced by
- `tests/unit/test_segment_profile.py`: every per-campaign roster rule, with paired controls; the
  planner accepting the campaign-and-segment grain and refusing campaign alone; one campaign's gap
  refuses only it; a truncated grouped read refuses the section; chat's single-campaign roster
  equals the grouped one.
- `tests/unit/test_audiences_presentation.py` and `tests/unit/test_audiences_parity.py`: byte
  bounds with wide rows, ordering, index shards, item, path and section absences, the body bound
  paired at the bound and one past it, and grouped rows equal to the per-campaign rows captured
  before the change.
- `tests/unit/test_preset_bakeability.py`: the count is constant in campaigns and derived, not
  typed; the body bound and its write seconds are derived; the gate's printed count equals the
  certified count.
- `tests/unit/test_presentation_handler.py`: the part request's accepted and refused bodies, an
  identity absent from the index, and the missing-child refusal.
- `frontend/test/audiences*.test.ts(x)`: fetch on selection only, at most one prefetch, and the
  revision reload.
- `tests/integration/test_audiences_runtime_bake.py`: equal statement counts at 1 and 60 campaigns.

## Consequences
- Every DSP campaign with delivery in the month is reachable on the page. Opening one is one baked
  body read with no measurement.
- The certified count drops from 61 to 24. Writes are certified at the body bound, so the derived
  budget rises from 300 s to 570 s. It's an alarm: the projected large seller writes about 668
  bodies, under 4% of the bound.
- The bake writes more, smaller objects through the streamed sibling writes of ADR-145.
- The render format and artifact version bump, so every Audiences pointer rebuilds once. Until
  that re-bake, the retained v1 body is the page's month-not-published absence (ABS-AUD-1),
  never decoded and never a 500 (dev 2026-10-25, D2).
- Chat's "outside the page's top-N" acceptance turn becomes a page-and-chat parity check on the
  lowest-ranked campaign.
- One label read per namespace replaces the per-campaign label reads that the stopgap deferred.
- New copy (per-campaign, per-path, section, fetch-failed and revision-changed states) needs the
  owner's copy approval. The page-level over-bound absence is retired.

## Alternatives considered
- **Raise the budget to the 900 s Lambda timeout.** Rejected by (1): still linear in campaigns, and
  the modelled count stops near 40 campaigns.
- **One history statement per older month.** Rejected: statements would grow with retained
  history, and a streamed reduction reads them together at constant count.
- **A new keys-only planner read.** Rejected: a new plan kind, when a reduction over an accepted
  grain gives the same keys.
- **Split Audiences into several bake tasks.** Rejected: the final transaction (`10 + N + 3 ×
  written` actions) passes DynamoDB's 100-action limit.
- **Raise the body cap.** Rejected: up to about 27.6 MB at the large profile, past the 6 MB Lambda
  response and 10 MB API Gateway payload limits.
- **Measure a campaign when the user opens it.** Rejected by (1): seconds per click against a 1 s
  serve target.
- **Precompute campaign rosters at ingestion.** Rejected: a larger blast radius than grouping, for
  the same count.

## Review trigger
A second marketplace on Audiences (keys become marketplace and campaign, one body axis per
marketplace inside the same task). A grouped campaign answer reaching the bake row ceiling
(ADR-143) at a real seller. Or a measured per-object write time above the 0.1 s budget, which
re-derives the body bound.
