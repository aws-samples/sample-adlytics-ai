# Audiences — high-level design

## 4. Solution strategy

One tab, one baked artifact, served from publish-time artifacts through the
[preset artifact publisher](../preset-artifact-publisher.md). The only preset is "Latest measured month",
so the page shows one complete calendar month and no date control
([information architecture](../information-architecture/README.md#6-shared-patterns): a page with one
preset shows no control, and the scope band names the window). This document records only what's specific
to this tab.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| Audiences | `POST /api/presentation/audiences` [proposal] | Session; optional `TemporalSelection.v2` restricted to `omitted` or `last_complete_month`; optionally one part (a campaign page, a profile page or a campaign index shard) | Resolve the month and serve its baked parent, or the one baked part asked for |
| Audiences metric focus | `POST /api/presentation/audiences-metric-focus` [proposal] | Session, a declared focusable metric, the selection | Derive the focus payload from the baked artifact |

The profile selector chooses among paths the parent already carries, so it doesn't re-request. Choosing a DSP
campaign, or another page of a profile or campaign, fetches that one baked part
([LLD](low-level-design.md#audienceprofileartifact-v2)). Both routes are new API Gateway routes and need their own `infra/` approval.

### Composition (top to bottom, per the mockup)

1. Hub header: "Reach and audiences" eyebrow, tab strip, h1 "Audiences", the description, then the scope
   band ("Showing August 2026 (Latest measured month).", fictional).
2. A paid-feature callout when the paid profile hasn't delivered, saying the DSP-exposed profile is shown
   instead.
3. **Segment profile**: the profile selector, then the segment table (segment and category, share of
   buyers, share of reached shoppers, index), with roster segments that didn't return listed last as "Not
   available".
4. **Insights row**: the `segment-over-index` card for the selected profile.
5. **DSP campaigns by segment**: the limit sentence, the campaign selector and the per-segment table.
6. **About these figures**.

### Data lineage

| Section | Source | Packs |
|---|---|---|
| Segment profile, DSP-exposed path | The segment-profile fact with `profile_path = dsp_exposed_only` | Standard tables, pack 12 |
| Segment profile, paid path | The segment-profile fact with `profile_path = asi_all_exposed` | Audience Segment Insights, packs 10 and 11, names from pack 13 |
| Segment names and categories | `dim_audience_segment`, namespaced `amc_dsp_behavior` or `amc_asi` | Packs 9 and 13 |
| DSP campaigns by segment | The segment-delivery fact (DSP only, campaign grain) | Pack 8 |

The two segment facts aren't in the [canonical model](../../canonical-model.md) yet; each joins it, with its
columns, in slice S3.1. `dim_audience_segment` is already listed there as proposed.

The two namespaces stay separate: P-BO-4 step 1 failed on the validation instance on 2026-10-09, so no DSP
segment is linked to an ASI segment, and the page says the two use different IDs
([results](../../../validation/RESULTS.md#p-bo-3-and-p-bo-4-on-the-standard-demo-instance)). Each segment's
provider label, Amazon or third party, comes from `dim_audience_segment`. Packs 10, 11 and 13 need the pinned `segment_marketplace_id` (US pinned on 2026-10-08).

### Build slice and dependencies

| Needs | Why |
|---|---|
| Slice S3.1, after the exposure fence of S2.2 | The packs, facts, dimension and segment roster |
| Paid-feature state (built in the domain) | The paid profile's three states |
| P-BO-2 | The array route for DSP segment performance; the per-segment table is the fallback |
| Open decision 5 (default: yes) | Whether demographic segments appear; "no" applies a governed exclusion list at load |
| A route approval | The two new routes |
