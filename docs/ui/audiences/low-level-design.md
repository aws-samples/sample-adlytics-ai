# Audiences — low-level design

## 6. Wire, artifact, and renderer contracts

Marker key: [repo] is read in this worktree on 2026-10-08 with `file:line`; [proposal] is a choice made in
this package that the build slice may still change through review.

### Exact API inputs

| Route | Method and auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/audiences` [proposal] | `POST`, the custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v2` restricted to `omitted` or `last_complete_month` | segment IDs, profile paths, campaign IDs or names, rolling presets, custom ranges, unknown fields |
| `/api/presentation/audiences-metric-focus` [proposal] | `POST`, same authorizer | `{session_id, metric, temporal_selection}` with a declared focusable metric | any segment, campaign or grain field, unknown fields |

### Page declarations

New page id `audiences`, route `/audiences/`, governed label "Audiences" for both the tab and the title, in
the Reach and audiences hub with tab order 2.

| File | Entry |
|---|---|
| `presentation/pages/v1/pages.yaml` | `mode: single_basis`, `grain.time: declared_period`, dimensions `[marketplace]`; metric slots for the profile and segment-delivery metrics, each with `period_grains: [month]` and the `time_grain` exact selector |
| `presentation/pages/v2/pages.yaml` | `mode: selectable`, `boundary_required: true`, `navigation: {hub_id: reach, tab_label: Audiences, tab_description: Which Amazon audience segments your buyers belong to, compared with the shoppers your ads reached, and how DSP campaigns did per segment, order: 2}`. `temporal_policy`: `candidate_presets: [last_complete_month]`, `default_preset: last_complete_month`, custom disabled, `comparison_policy: prior_calendar_period` (same metric, contract, grain set and profile path across two months), `display_grains: [declared_period]`. Artifact `AudienceProfileArtifact` v1, outcomes `[complete, empty, insufficient]` |

| Section | `declared_periods` | `period_anchor` | Notes |
|---|---|---|---|
| `workspace-scope` | none | n/a | |
| `profile-path` | none: binds no metric | n/a | Which profile is shown, and the paid-feature callout |
| `segment-ranking` | `last_complete_month` | true | The profile table; the only anchor |
| `audience-insights` [proposal] | none: binds findings, not metrics | n/a | `segment-over-index` |
| `campaign-audiences` | `last_complete_month` | false | A late pack 8 shows its pending text for the same month |
| `evidence-trace`, `boundary` | none | n/a | |

### `AudienceProfileArtifact` v1

| Section | Content | Rendering responsibility |
|---|---|---|
| Profile path | The two paths with their paid-feature states, and the default path the server chose | Segmented control "Profile" inside the section; switching selects a server-served path, with no re-request |
| Segment ranking | Per segment, ordered by index on the server: display name, `provider_label`, `category_level_1` and `category_path` (paid only), `segment_buyer_share`, `segment_exposed_share`, `segment_index`, and `segment_buyers_new_to_brand` for the new-to-brand view; only segments with `segment_buyers` at or above the contract's display minimum carry an index | Table caption "{path} · {month} · segment figures never add up", then the segment-ID sentence below; the provider label under each segment name; roster segments with no row this month listed last as "Not available" with their reason |
| Insights | Fixed roster `segment-over-index` v1 per profile path | One card per path |
| Campaign audiences | Per DSP campaign with delivery in the month, per segment: `provider_label`, `segment_impressions`, `segment_cpm`, `segment_roas_attributed` with its basis label. Each campaign carries `campaign_label`, its governed name or chat's placeholder "Unnamed campaign N", and `campaign_ref`, an opaque ordinal within the artifact; its canonical key never reaches the artifact. At most the campaign bound per marketplace (below), ranked; `shown_text` when pack 8 listed more | Campaign selector keyed by `campaign_ref`, the limit sentence, the server's "Showing {shown} of {total} campaigns" when served, and the table, with the provider label under each segment name |
| Evidence status | `EvidenceStatus.v2` | Coverage sheet |
| Page context and starter questions | As the other tabs | Assistant |
| Boundary | "This describes an audience you can build in your own tools. Adlytics doesn't create or activate audiences." and the segment-ID sentence below | "About these figures", collapsed, on every outcome: complete, empty and insufficient |

**Server-owned display fields.** The artifact carries `segment_buyers_text`,
`segment_buyers_new_to_brand_text`, `segment_impressions_text`, `segment_index_text`,
`segment_cpm.text`, and `segment_roas_attributed_text`; the browser renders those strings and
never formats the corresponding raw values. Each paid delivery carries `period_text` beside its
baked `paid_state`. The presentation route resolves that text at request time: pending through
`expected_after`, missing on the following day, with no rebake. Published deliveries and the DSP
path carry no `period_text`. The page renders the artifact's `month.label` as its scope band on
complete outcomes and before campaign rows on non-complete outcomes; the browser never derives or
formats that month.

**Campaign and body bounds (owner decision 2026-10-14, integration gate IG-002 and IG-003).** One bake
must fit the baker's timeout and one body must fit the 256,000-byte artifact cap, so the page shows a bounded
set of campaigns. **Known limit (dev-only stopgap, owner-approved 2026-10-17; TD-D2-AUDIENCES-CAMPAIGN-LIMIT in
`validation/README.md`, tracked by the outcome-first redesign):** the page shows at most
`audiences_runtime_campaign_bound` campaigns, the certifier's bound, 7 at one marketplace (61 certified
plans). The figures below describe `audiences_timeout_headroom_campaigns`, a 900 s timeout estimator that
is not the page limit. It derives its count from the bake budget
model. Per marketplace, one bake runs the campaign list, then the profile and campaign roster
reads, then the profile and campaign measurements as three separate barriers at the deployed
concurrency. The model sums each phase's wave ceiling and reserves separate render/finalization,
body-write and positive non-query margins. With the one governed marketplace the estimator gives 11.
Campaigns rank on the server by their largest matched-segment impressions, descending, then by campaign key,
the order that numbers `campaign_ref`. The rank never sums segment impressions, because segment figures
never add up. When pack 8 listed more campaigns than the page shows, the artifact carries the server's
`shown_text`, exactly "Showing {shown} of {total} campaigns". The browser renders it and computes nothing.
`audiences_row_budget` derives the most profile and campaign rows a body holds from per-marketplace envelope
and per-row byte ceilings, which tests hold with wide rows. A month over that budget, or one whose encoded
body exceeds the cap, publishes a small typed `AudienceProfileUnavailable` body with the named reason
`audiences_body_over_bound` and advances the target month's pointer. It is never truncated, and its pointer
supersedes the prior month's pointer so an older month is never described as latest. Source composition and
the renderer also refuse any marketplace cardinality other than exactly one before a query port is called.
This bound is page presentation only. Chat's governed access reads every campaign, as before.

**Provider labels (revised at `2026-10-09.2`, owner decision 2 of 2026-10-09 about 02:40 UTC).** Every
segment carries `provider_label`, "Amazon" or "Third party", read from the governed provider classification
of `dim_audience_segment` (business outcomes wave 2b, FEAT-008). The page never labels all segments
"Amazon's segments": P-BO-3 found demographic segments in the DSP segment table, and some of them are from
onboarded third-party data providers [inference from the names]
([results](../../../validation/RESULTS.md#p-bo-3-and-p-bo-4-on-the-standard-demo-instance)).
A segment the classification doesn't cover carries no label rather than a guessed one [proposal]. The
profile name "Amazon audience segments (paid)" stays: it names the Audience Segment Insights feature, not who
provides each segment.

**Segment IDs (revised at `2026-10-09.2`).** DSP behaviour segments (the "DSP-exposed buyers only" profile
and DSP campaigns by segment) and Amazon audience segments (the paid profile) use different IDs. P-BO-4 step 1
failed on the validation instance on 2026-10-09: too few IDs matched, and some names appear under a
different ID ([results](../../../validation/RESULTS.md#p-bo-3-and-p-bo-4-on-the-standard-demo-instance);
[ADR-103](../../decisions/adr-103-model-audience-segments-as-a-source-namespaced-overlapping-dimension.md)
point 6). So the two kinds are never linked, joined by name or compared as one list. The segment ranking
caption and "About these figures" both carry the server sentence "DSP behaviour segments and Amazon audience
segments (paid) use different IDs, so they're never linked or compared as one list." [proposal]

**Shares and indexes stay inside one delivery.** If a delivery's `profiled_total` row is missing, every share
and index of that delivery reads "Not available". The two profile paths are never compared as one series.

**The new-to-brand view.** The business outcomes design gives the segment ranking a new-to-brand toggle, and
the governed metric `segment_buyers_new_to_brand` exists for it. The approved mockup doesn't draw the toggle.
Under the owner's render-more principle it's kept: a two-option segmented control "All buyers" / "New-to-brand
buyers" [proposal] inside the section header. Its conformance is checked against this document, not the
mockup.

### Segment roster

The roster is load-side metadata: the segments returned for the same marketplace and path in the previous
month.

- A roster segment with no row this month is listed last as "Not available" ("Amazon returned too few shoppers
  for this segment this month. It was listed last month."), never as index 0. The server-owned reason renders
  once in the row's first metric cell; the other metric cells show an assistive-technology-hidden `—` and
  reference that reason with `aria-describedby`. A missing reason is a malformed artifact, never a bare marker.
- A returned segment not on the roster is shown, and joins next month's roster.
- Month 1 has an empty roster; the section notes "First month: segments with too few shoppers aren't listed."
- There's no "all other" row. Roster loss is graded and shown as a degraded note, never withdrawn, because no
  coarser view of segments exists.

### Designed states

| Condition | What renders |
|---|---|
| Paid profile not observed (packs 10, 11 or 13) | The callout "Available with the paid feature" with the server text, for example "Needs the Amazon Insights paid feature (Audience Segment Insights). No data has arrived for this workspace."; the page defaults to "DSP-exposed buyers only" |
| Paid profile chosen while not observed | The paid option stays in the selector and shows its callout in place of the table |
| Packs 10–11 observed, pack 13 not | ASI segments render their ID with "name not available yet"; the index still renders |
| Month not landed or didn't arrive | The shared pending and missing texts |
| Pack 8 late | `campaign-audiences` shows its pending text; the profile still renders |
| Profile insufficient or empty, campaign rows published | The profile callout, then `campaign-audiences` with its rows: campaign evidence is never hidden by the profile outcome |
| Open decision 5 = no | Demographic segments are removed at load by a governed exclusion list; nothing changes on the page |
| A segment the provider classification doesn't cover | The segment renders with no provider label [proposal] |
| No published body for the month, or the month's body is over the page's bound (page-level) | Page-level absence: [ABS-AUD-1](../page-absence-copy.md#abs-aud-1) or [ABS-AUD-2](../page-absence-copy.md#abs-aud-2); no scope band |

The callout never says the customer isn't subscribed.

### Insight contract: `segment-over-index` v1

Perspective class. It names top segments by index only where `segment_buyers` is at least `B_min` (policy,
proposed 30) **and** the lower bound of buyer share minus exposed share is above zero, with a Bonferroni
critical value over the number of segment rows in that delivery and path. The z value comes from
`statistics.NormalDist`, so there's no new dependency. Calibration rejects summing segment shares, and a
fixture of 200 segments with equal buyer and exposed shares names none. Each card says which profile it reads
and "Only segments with at least {B_min} buyers are named."

### "Also changed"

The alerts-declined path applies (OD-7). No default alert rule covers `segment-over-index`
([ADR-108](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)), so in v1 this tab
has no "Also changed" section. A later rule brings it in after the insights row with no page change.

### Materializer

New, under the ADR-075 pattern. It bakes on pack 8–13 publication transitions for the latest complete month,
with both profile paths and every DSP campaign's segment rows in one artifact, and bakes its deterministic
findings.

## 7. Concepts applied

- [Business outcome measurement](../../concepts/business-outcome-measurement.md)
- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)

## 8. Decisions

- [ADR-103: audience segments as an overlapping, namespaced dimension](../../decisions/adr-103-model-audience-segments-as-a-source-namespaced-overlapping-dimension.md)
- [ADR-107: paid-feature availability from delivery evidence](../../decisions/adr-107-derive-paid-feature-availability-from-delivery-evidence.md)
- [ADR-100: complete measured periods](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)
- [ADR-124: the render-more rule](../../decisions/adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Page declaration | v1 and v2 entries pass `make validate-page-schemas` |
| Overlap rule | A planner test refuses a sum of segment figures; the table caption and "About these figures" say segment figures never add up |
| Provider labels | Unit tests: an Amazon-provided segment carries "Amazon" and a third-party one "Third party", each from the governed classification (paired); no server text on the page reads "Amazon's segments" |
| Segment IDs | The ranking caption and "About these figures" carry exactly the segment-ID sentence; a test refuses any artifact row, finding or starter question that links a DSP segment to an ASI segment, paired with each path rendering on its own |
| Roster | Unit tests: a missing roster segment lists as "Not available", a new segment shows, month 1 shows the first-month note, 30% churn under `B_min` isn't withdrawn |
| Paid states | Unit tests for every paid-feature state, including partial enrolment |
| Insight | `segment-over-index` v1 with its calibration corpus |
| Read-only | No control on the page writes to Amazon or any other system |
| Conformance | Rule 4 of the [delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) against every mockup state, and against this document for the new-to-brand view, the provider labels and the segment-ID sentence, which the mockups don't draw |
| Live | Deployed dev bake observed end to end and recorded in `validation/RESULTS.md`; real AMC figures wait on the probes and the customer's enrolment |

## 10. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| Provider labels (revised at `2026-10-09.2`). The approved mockup shows no provider label; this document adds "Amazon" or "Third party" under each segment name in both tables, and none for an unclassified segment | Owner ratification of manifest revision `2026-10-09.2` (wave 2b stop item S-B1) | Nothing; the build uses this document |
| Segment-ID sentence (revised at `2026-10-09.2`). "DSP behaviour segments and Amazon audience segments (paid) use different IDs, so they're never linked or compared as one list.", in the ranking caption and "About these figures"; the approved mockup has neither | Owner ratification (S-B1) | Nothing |
| Tab description. The approved description reads "Which Amazon audience segments your buyers belong to, …", which can read as every segment being Amazon's. This revision keeps it, because the information architecture owns it | Owner ratification (S-B1): keep it, or drop "Amazon" in an information-architecture revision | Nothing |
