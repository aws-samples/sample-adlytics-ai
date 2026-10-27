# ADR-127: Serve the Campaigns channel filter from render-only siblings of one bake
Date: 2026-10-07
Status: Proposed. Two parts were put to the owner as open decisions with recommendations and taken as
recommended, without owner objection, when the workflow orchestrator approved the design on 2026-10-07
under the owner's delegated overnight authorization: the channel filter with a "Sponsored ads (all
sponsored channels)" option and a `30d` preset, and never totalling attributed figures across channels.
Delivery per channel renders under the owner's direction of 2026-10-07 that channel delivery renders if
its data exists. Implemented in code on 2026-10-08 (Slice 8,
[delivery slices](../ui/information-architecture/delivery-slices.md#3-slices)) and not yet deployed,
so the slice's live checks on dev are pending. Amends
[ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md) (the `30d` preset
returns, see its 2026-10-08 amendment) and
[ADR-071](adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md) when accepted.

> **Proposed amendment, 2026-10-18.** [ADR-145](adr-145-serve-ten-thousand-campaigns-with-a-sharded-roster-and-streamed-writes.md) (Proposed) would shard each filter sibling's name roster into ordered sibling bodies and move every filter's segment descriptors from its custody into one sharded custody digest index per publication. Implemented 2026-10-26 by the scale-limits change (PR #20) and live-validated on dev: where it differs from this decision, the amendment describes the code. ADR-145's acceptance is the owner's review.

## Context

The owner's coverage gap 1 is a DSP versus sponsored split and filter. Campaigns has no channel filter
today. A pointer binds exactly one body and one custody digest
[repo: `src/domain/presentation/preset_artifacts.py:422-431`]; body and custody keys are
`pages/{page}/{variant}/{revision}.json` and `custody/{page}/{variant}/{revision}.json` (`:410`,
`:379`); and segment identity is `{ordering}/segment-NNNN` (`:215-229`). None has a filter dimension.
Under today's baker each variant runs its own measurement batch
[repo: `docs/ui/preset-artifact-publisher.md:40-45`]. Insight slots are cached by revision
[repo: `src/adapters/inbound/presentation_handlers/handler.py:3704-3705`].

One purchase can be credited to more than one channel, so attributed purchases and ROAS can't be
totalled across channels. Spend, impressions and delivery ratios aren't attributed, and clicks, CTR,
CPC and CPM are valid at `channel` grain [repo: `contracts/shared/metric-dictionary.yaml:146-160`,
`:745-817`].

## Decision

**Each preset is measured once. Each channel filter value is a render-only sibling of that preset's
artifact, rendered from the same batch in the same bake, under one pointer.**

1. **Filters.** A closed, server-declared set per workspace: `all`; `sponsored` ("Sponsored ads (all
   sponsored channels)") when two or more sponsored channels are present; and each governed channel
   present. Plus a `30d` preset.
2. **Custody.** The pointer keeps its shape and binds the All-channels body and custody. All-channels
   custody gains a closed `filter_siblings: { <filter_id>: { body_digest, custody_digest } }` set.
   Sibling keys are `pages/{page}/{variant}/{revision}/filters/{filter_id}.json` and the matching
   custody key. Segment identity becomes `{filter_id}/{ordering}/segment-NNNN`. Every sibling is
   written and digest-checked before the pointer advances. A missing sibling or digest mismatch is the
   malformed-artifact refusal, never a fallback to All channels.
3. **What a sibling carries.** Every sibling has its own rows, segments, name roster for "Find a
   campaign", baked deterministic findings and insight slots. A one-channel sibling has today's tiles
   and trends for that channel. A multi-channel sibling (`all`, `sponsored`) has spend and impressions
   totals with changes; a "By channel" table of spend and impressions per channel; purchases and ROAS
   as in-place "Not added across channels" tiles with a reason and a link to the Scorecard; and a
   spend-only trend. It has no attributed comparison of any kind.
4. **Delivery per channel.** Multi-channel siblings also carry, per channel, clicks, CTR, CPC and CPM,
   each with its prior-window value and server `change`
   ([ADR-121](adr-121-declare-change-direction-and-sentiment-on-the-server.md)), and, when the option
   includes Amazon DSP, the viewable rate with its covered days. Sponsored rows show "—" with the reason
   "Not available: sponsored ads don't report viewability", never zero. A partly reported day follows
   the one rule in
   [ADR-118](adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md) decision
   5. No new measurement batch: the batch already reads clicks, because campaign rows serve CTR
   [repo: `src/domain/presentation/campaign_portfolio.py:88-93`, CTR at `:92`], and clicks' datasets
   are declared at `metric-dictionary.yaml:159`. Viewability adds the DSP views dataset to the DSP part
   of the batch.
5. **Insights.** The insight cache key keeps every existing field and adds `filter_id` (`all` on pages
   without filters). On a multi-channel sibling, `movers` runs on single-campaign changes, `exposure`
   and `contribution` use spend only, and `divergence` and the purchases and ROAS focus slots don't
   run.
6. **Requests.** `campaigns`, `portfolio-metric-focus` and `insight-deep-dive` gain `filter_id`;
   omitted means `all`, and an undeclared value is refused. The URL holds `preset` and `channel` as
   governed ids, and a default is never written. A well-formed id the preset doesn't offer is refused
   with its own code, `ADLY-PRESENTATION-0005`, so the browser shows All channels for that refusal
   alone: a link naming a channel the tab doesn't offer, or a window change to a cohort without the
   channel on screen, each with its own sentence. The refused id then leaves the URL and later
   requests (implementation note, 2026-10-08).

## Consequences

- Four measurement batches per publication instead of three, and up to seven render-only siblings per
  preset, which the low-level design checks against the bakeability gate. At the 1,000-campaign bound (removed 2026-10-25 by
  [ADR-152](adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md) decision 8)
  a variant writes at most 1,300 immutable objects (2 × 10 orderings × 65 segments). The gate checks
  that count against the baker's own memory and timeout, not against itself, and the local estate
  acceptance bakes seven siblings at the bound and holds its object count equal to the gate's count
  ([operational design](../ui/campaign-portfolio/operational-design.md) §1).
- Campaign rows no longer carry a spend spark (render format 11). A 30-day spark on 50 rows put the
  served body over the 256,000-byte cap, and the approved Campaigns table has no trend column; a
  campaign's daily spend has its home in detail.
- Every filter of a preset shares one window and revision, so one pointer read serves every filter's
  drill-link text, and a Scorecard channel row lands on its own channel.
- Model calls grow with the filter values present, because slots fill per sibling.
- A test checks that two siblings of one revision with the same slot and subject cache separately, and
  that a prompt activation invalidates both.

## Alternatives considered

- **Key variants by preset and filter.** Up to 24 variants, each with its own batch, so up to eight times
  today's measurement, and All channels and one channel could end on different windows.
- **Filter in the browser.** The browser would aggregate, which it never does.
- **Total purchases and ROAS across channels with a double-counting note.** Leaves a figure a CMO might
  quote.

## Review trigger

Reopen when a workspace has more governed channels than the bakeability gate allows, or a second page
needs filters.
